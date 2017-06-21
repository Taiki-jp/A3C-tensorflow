import numpy as np
import tensorflow as tf
import threading

import signal

class ActorLearnerThread(threading.Thread):
  def __init__(self, session, environment, shared_network, local_network, thread_id, device='/cpu:0'):
    super(ActorLearnerThread, self).__init__()
    self.session = session
    self.t_max = 10
    self.shared_network = shared_network
    self.local_network = local_network
    self.t = 1
    self.t_start = self.t
    self.stop = False
    self.image_width = local_network.input_shape()[0]
    self.image_height = local_network.input_shape()[1]
    self.num_channels = local_network.input_shape()[2]
    self.skip_num = 4
    self.eps = 1e-10
    self.beta = 0.01
    self.gamma = 0.99
    self.grad_clip = 40
    self.saver = tf.train.Saver({var.name: var for var in self.local_network.weights_and_biases()})
    self.device = device

    self.environment = environment

    self.state_input, self.action_input, self.reward_input = self.prepare_placeholders(thread_id)
    self.pi = self.local_network.pi(self.state_input)
    self.value = self.local_network.value(self.state_input)
    self.policy_loss, self.value_loss = self.prepare_loss_operations(thread_id)
    self.local_grads = self.prepare_local_gradients(thread_id)
    self.reset_local_grads_ops = self.prepare_reset_local_gradients_ops(self.local_grads, thread_id)

    total_loss = self.policy_loss + self.value_loss * 0.5
    self.accum_local_grads_ops = self.prepare_accum_local_gradients_ops(self.local_grads, total_loss, thread_id)
    self.apply_grads = self.prepare_apply_gradients(self.local_grads)

    # Signal handler for handling interrupt by Ctrl-C
    signal.signal(signal.SIGINT, self.signal_handler)


  def prepare_placeholders(self, thread_id):
    scope_name = "thread_%d_placeholder" % thread_id
    with tf.variable_scope(scope_name):
      state_shape=[None] + list(self.local_network.input_shape())
      assert state_shape == [None, self.image_width, self.image_height, self.num_channels]
      state_input = tf.placeholder(tf.float32, shape=state_shape, name="state_input")

      action_shape=[None] + list(self.local_network.actor_output_shape())
      assert action_shape == [None, 4]
      action_input = tf.placeholder(tf.float32, shape=action_shape, name="action_input")

      reward_shape=[None] + list(self.local_network.critic_output_shape())
      assert reward_shape == [None, 1]
      reward_input = tf.placeholder(tf.float32, shape=reward_shape, name="reward_input")

      return state_input, action_input, reward_input


  def prepare_loss_operations(self, thread_id):
    scope_name = "thread_%d_operations" % thread_id
    with tf.name_scope(scope_name):
      pi, value = self.local_network.pi_and_value(self.state_input)
      log_pi = tf.log(self.pi + self.eps)
      entropy = tf.reduce_sum(tf.mul(pi, log_pi), reduction_indices=1, keep_dims=True)

      pi_a_s = tf.reduce_sum(tf.mul(pi, self.action_input), reduction_indices=1, keep_dims=True)
      log_pi_a_s = tf.log(pi_a_s)

      advantage = self.reward_input - value

      # log_pi_a_s * advantage. This multiplication is bigger then better
      # append minus to use gradient descent as gradient ascent
      policy_loss = - tf.reduce_sum(log_pi_a_s * advantage) + tf.reduce_sum(entropy * self.beta)
      value_loss = tf.reduce_sum(tf.square(advantage))

      return policy_loss, value_loss


  def prepare_local_gradients(self, thread_id):
    scope_name = "thread_%d_grads" % thread_id
    local_grads = []
    with tf.name_scope(scope_name):
      for variable in self.local_network.weights_and_biases():
        name = variable.name.replace(":", "_") + "_local_grad"
        shape = variable.get_shape().as_list()
        local_grad = tf.Variable(tf.zeros(shape, dtype=variable.dtype), name=name, trainable=False)
        local_grads.append(local_grad.ref())
    assert len(local_grads) == 10
    return local_grads


  def prepare_accum_local_gradients_ops(self, local_grads, dy, thread_id):
    scope_name = "thread_%d_accum_ops" % thread_id
    accum_grad_ops = []
    with tf.device(self.device):
      with tf.name_scope(scope_name):
        dxs = [v.ref() for v in self.local_network.weights_and_biases()]
        grads = tf.gradients(dy, dxs,
            gate_gradients=False,
            aggregation_method=None,
            colocate_gradients_with_ops=False)
        for (grad, var, local_grad) in zip(grads, self.local_network.weights_and_biases(), local_grads):
          name = var.name.replace(":", "_") + "_accum_grad_ops"
          accum_ops = tf.assign_add(local_grad, grad, name=name)
          accum_grad_ops.append(accum_ops)
    assert len(accum_grad_ops) == 10
    return tf.group(*accum_grad_ops, name="accum_ops_group_%d" % thread_id)


  def prepare_reset_local_gradients_ops(self, local_grads, thread_id):
    scope_name = "thread_%d_reset_ops" % thread_id
    reset_grad_ops = []
    with tf.device(self.device):
      scope_name = "thread_%d_reset_operations" % thread_id
      with tf.name_scope(scope_name):
        for (var, local_grad) in zip(self.local_network.weights_and_biases(), local_grads):
          zero = tf.zeros(var.get_shape().as_list(), dtype=var.dtype)
          name = var.name.replace(":", "_") + "_reset_grad_ops"
          reset_ops = tf.assign(local_grad, zero, name=name)
          reset_grad_ops.append(reset_ops)
    assert len(reset_grad_ops) == 10
    return tf.group(*reset_grad_ops, name="reset_grad_ops_group_%d" % thread_id)


  def prepare_apply_gradients(self, local_grads):
    clipped_grads = [tf.clip_by_value(grad, -self.grad_clip, self.grad_clip) for grad in local_grads]
    apply_grads = self.shared_network.optimizer.apply_gradients(
        zip(clipped_grads, self.shared_network.weights_and_biases()),
        global_step=self.shared_network.shared_counter)
    return apply_grads


  def run(self):
    available_actions = self.environment.available_actions()
    while tf.train.global_step(self.session, self.shared_network.shared_counter) < self.t_max \
        and self.stop == False:
      self.reset_gradients()
      self.sync_network_parameters(self.shared_network, self.local_network)
      self.t_start = self.t
      initial_state = self.get_initial_state()

      self.environment.reset()
      history, last_state = self.play_game(initial_state)

      if last_state is None:
        r = 0
      else:
        r = self.session.run(self.value, feed_dict={self.state_input : [last_state]})[0][0]

      states_batch = []
      action_batch = []
      reward_batch = []
      for i in range((self.t - 1) - self.t_start, -1, -1):
        snapshot = history[i]
        state, action, reward = self.extract_history(snapshot)

        r = reward + self.gamma * r
        states_batch.append(state)
        action_batch.append(action)
        reward_batch.append([r])

      self.accumulate_gradients(states_batch, action_batch, reward_batch)

      self.update_shared_gradients()


  def extract_history(self, history):
    state = history['state']
    action = np.zeros(self.local_network.actor_outputs)
    action[history['action']] = 1
    reward = history['reward']
    return state, action, reward


  def get_initial_state(self):
    initial_state = []
    available_actions = self.environment.available_actions()
    while True:
      next_screen = None
      for i in range(self.skip_num):
        action = self.select_random_action_from(available_actions)
        reward, next_screen = self.environment.act(action)

      initial_state.append(next_screen)
      if len(initial_state) is self.num_channels:
        break
    return initial_state


  def sync_network_parameters(self, origin, target):
    copy_operations = [target.assign(origin)
        for origin, target in zip(origin.weights_and_biases(), target.weights_and_biases())]
    self.session.run(copy_operations)


  def play_game(self, initial_state):
    history = []
    state = np.stack(initial_state, axis=-1)
    next_state = state
    next_screen = None
    action = 0
    reward = 0
    available_actions = self.environment.available_actions()
    while self.environment.is_end_state() == False and (self.t - self.t_start) != self.t_max:
      state = next_state
      probabilities = self.session.run(self.pi, feed_dict={self.state_input : [state]})
      action = self.select_action_with(available_actions, probabilities[0])

      reward += 0.0
      for i in range(self.skip_num):
        intermediate_reward, next_screen = self.environment.act(action)
        reward += np.clip([intermediate_reward], -1, 1)[0]

      data = {'state':state, 'action':action, 'reward':reward}
      history.append(data)
      next_screen = np.reshape(next_screen, (self.image_width, self.image_height, 1))
      next_state = np.append(state[:, :, 1:], next_screen, axis=-1)

      self.t += 1

    if self.environment.is_end_state():
      last_state = None
    else:
      last_state = next_state

    print 'self.t_start: %d, self.t: %d' % (self.t_start, self.t)
    return history, last_state


  def synchronize_network(self):
    self.sync_network_parameters(self.shared_network, self.local_network)


  def accumulate_gradients(self, state, action, r):
    self.session.run(self.accum_local_grads_ops,
       feed_dict={self.state_input: state, self.action_input: action, self.reward_input: r})


  def reset_gradients(self):
    self.session.run(self.reset_local_grads_ops)


  def update_shared_gradients(self):
    self.session.run(self.apply_grads)


  def save_parameters(self, file_name, global_step):
    self.saver.save(self.session, save_path=file_name, global_step=global_step)


  def select_action_with(self, available_actions, probabilities):
    return np.random.choice(available_actions, p=probabilities)


  def select_random_action_from(self, available_actions):
    return np.random.choice(available_actions)


  def signal_handler(self, signal, frame):
    self.stop = True
    print 'Thread interrupted... stopping training'
