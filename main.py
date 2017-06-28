from constants import IMAGE_WIDTH
from constants import IMAGE_HEIGHT
from constants import NUM_CHANNELS
from constants import NUM_ACTIONS

import gflags
import sys
import os
import time
import re
import numpy as np
import tensorflow as tf
import a3c_network as a3c
import shared_network as shared
import actor_learner_thread as actor_thread
import ale_environment as ale

FLAGS = gflags.FLAGS
gflags.DEFINE_string('summary_dir', 'summary', 'Target summary directory')
gflags.DEFINE_string('checkpoint_dir', 'checkpoint', 'Target checkpoint directory')
gflags.DEFINE_string('rom', 'breakout.bin', 'Rom name to play')
gflags.DEFINE_integer('threads_num', 8, 'Threads to create')
gflags.DEFINE_integer('global_t_max', 1e9, 'Max steps')
gflags.DEFINE_boolean('use_gpu', True, 'True to use gpu, False to use cpu')


def merged_summaries(maximum, median, average):
  max_summary = tf.scalar_summary('rewards max', maximum)
  med_summary = tf.scalar_summary('rewards med', median)
  avg_summary = tf.scalar_summary('rewards avg', average)
  return tf.merge_summary([max_summary, med_summary, avg_summary])


previous_time = time.time()
previous_step = 0
def loop_listener(thread, iteration):
  global previous_time
  global previous_step
  ITERATION_PER_EPOCH = 2000
  current_time = time.time()
  current_step = thread.get_global_step()
  elapsed_time = current_time - previous_time
  steps = (current_step - previous_step) * 20
  print("itearation: %d, previous step: %d" % (iteration, previous_step))
  print("### Performance: {} steps in {:.5f} seconds. {:.0f} STEPS/s. {:.2f}M STEPS/hour".format(
    steps, elapsed_time, steps / elapsed_time, steps / elapsed_time * 3000 / 1000000.))
  previous_time = current_time
  previous_step = current_step
  if (iteration % ITERATION_PER_EPOCH) == 0:
    with ale.AleEnvironment(FLAGS.rom, record_display=False, show_display=True, id=100) as environment:
      trials = 10
      rewards = thread.test_run(environment, trials)
      maximum = np.max(rewards)
      median = np.median(rewards)
      average = np.average(rewards)
      epoch = iteration / ITERATION_PER_EPOCH
      summary_writer.add_summary(session.run(summary_op,
        feed_dict={maximum_input: maximum, median_input: median, average_input: average}),
        epoch)
      print 'test run for epoch: %d. max: %d, med: %d, avg: %f' % (epoch, maximum, median, average)

  if (iteration % ITERATION_PER_EPOCH) == 0:
    step = thread.get_global_step()
    print 'Save network parameters! step: %d' % step
    thread.save_parameters(FLAGS.checkpoint_dir + '/network_parameters', step)


def create_dir_if_not_exist(directory):
  if not os.path.exists(directory):
    os.makedirs(directory)


def remove_old_files(directory):
  for file in os.listdir(directory):
    os.remove(os.path.join(directory, file))


if __name__ == '__main__':
  try:
    argv = FLAGS(sys.argv)
  except gflags.FlagsError:
    print 'Incompatible flags were specified'

  graph = tf.Graph()
  config = tf.ConfigProto()

  # Output to tensorboard
  create_dir_if_not_exist(FLAGS.summary_dir)
  remove_old_files(FLAGS.summary_dir)
  summary_writer = tf.train.SummaryWriter(FLAGS.summary_dir, graph=graph)

  # Model parameter saving
  create_dir_if_not_exist(FLAGS.checkpoint_dir)
  remove_old_files(FLAGS.checkpoint_dir)

  networks = []
  shared_network = None
  summary_op = None

  with graph.as_default():
    maximum_input = tf.placeholder(tf.int32)
    median_input = tf.placeholder(tf.int32)
    average_input = tf.placeholder(tf.int32)
    summary_op = merged_summaries(maximum_input, median_input, average_input)
    device = '/gpu:0' if FLAGS.use_gpu else '/cpu:0'
    shared_network = shared.SharedNetwork(IMAGE_WIDTH, IMAGE_HEIGHT, NUM_CHANNELS, NUM_ACTIONS, 100, device)
    for i in range(FLAGS.threads_num):
      network = a3c.A3CNetwork(IMAGE_WIDTH, IMAGE_HEIGHT, NUM_CHANNELS, NUM_ACTIONS, i, device)
      networks.append(network)

  with tf.Session(graph=graph, config=config) as session:
    threads = []
    for thread_num in range(FLAGS.threads_num):
      show_display = True if (thread_num == 0) else False
      environment = ale.AleEnvironment(FLAGS.rom, record_display=False, show_display=show_display, id=thread_num)
      thread = actor_thread.ActorLearnerThread(session, environment, shared_network,
          networks[thread_num], FLAGS.global_t_max, thread_num)
      thread.daemon = True
      if thread_num == 0:
        thread.set_loop_listener(loop_listener)
      threads.append(thread)

    session.run(tf.initialize_all_variables())

    for i in range(FLAGS.threads_num):
      threads[i].start()

    while True:
      try:
        ts = [thread.join(10) for thread in threads if thread is not None and thread.isAlive()]
      except KeyboardInterrupt:
        print 'Ctrl-c received! Sending kill to threads...'
        for thread in threads:
          thread.kill_received = True
        break

    print 'Training finished!!'
