import os
import math
import numpy as np
import tensorflow as tf
from tqdm import tqdm
from argparse import ArgumentParser
from models.LPNet import LPNet
from utils.prepro_activitynet import prepro_activitynet
from utils.data_utils import load_video_features, load_json, write_json, batch_iter
from utils.runner_utils import write_tf_summary, eval_test, get_feed_dict
import json

parser = ArgumentParser()
parser.add_argument("--gpu_idx", type=str, default="0", help="GPU index")
parser.add_argument("--seed", type=int, default=12345, help="random seed")
parser.add_argument("--mode", type=str, default="train", help="prepro | train | test")
parser.add_argument("--feature", type=str, default="new", help="[new | org]")
parser.add_argument(
    "--root", type=str, default="data/ActivityNet", help="root directory for store raw data"
)
parser.add_argument(
    "--wordvec_path", type=str, default="data/glove.840B.300d.txt", help="glove word embedding path"
)
parser.add_argument("--home_dir", type=str, default=None, help="home directory for saving models")
parser.add_argument(
    "--save_dir", type=str, default=None, help="directory for saving processed dataset"
)
parser.add_argument("--num_train_steps", type=int, default=None, help="number of training steps")
parser.add_argument("--char_size", type=int, default=None, help="number of characters")
parser.add_argument("--epochs", type=int, default=100, help="number of epochs")
parser.add_argument("--batch_size", type=int, default=16, help="batch size")
parser.add_argument("--word_dim", type=int, default=300, help="word embedding dimension")
parser.add_argument(
    "--video_feature_dim", type=int, default=1024, help="video feature input dimension"
)
parser.add_argument("--char_dim", type=int, default=100, help="character dimension")
parser.add_argument("--hidden_size", type=int, default=256, help="hidden size")
parser.add_argument("--max_position_length", type=int, default=512, help="max position length")
parser.add_argument(
    "--highlight_lambda", type=float, default=5.0, help="lambda for highlight region"
)
parser.add_argument("--extend", type=float, default=0.1, help="highlight region extension")
parser.add_argument("--num_heads", type=int, default=8, help="number of heads")
parser.add_argument("--drop_rate", type=float, default=0.1, help="dropout rate")
parser.add_argument("--clip_norm", type=float, default=1.0, help="gradient clip norm")
parser.add_argument("--init_lr", type=float, default=0.0001, help="initial learning rate")
parser.add_argument("--warmup_proportion", type=float, default=0.0, help="warmup proportion")
parser.add_argument("--period", type=int, default=100, help="training loss print period")
parser.add_argument("--eval_period", type=int, default=37421, help="evaluation period")
configs = parser.parse_args()

# os environment
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["CUDA_VISIBLE_DEVICES"] = configs.gpu_idx

np.random.seed(configs.seed)
tf.set_random_seed(configs.seed)
tf.random.set_random_seed(configs.seed)


class MyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(MyEncoder, self).default(obj)


# specify the dataset directory
if configs.home_dir is None:
    configs.home_dir = "ckpt/activitynet_{}_{}".format(configs.feature, configs.max_position_length)
configs.save_dir = "datasets/activitynet_{}/{}".format(configs.feature, configs.max_position_length)
configs.video_feature_dim = 1024 if configs.feature == "new" else 500

if configs.mode.lower() == "prepro":
    prepro_activitynet(configs)

elif configs.mode.lower() == "train":
    video_feature_path = os.path.join(
        configs.root, "activitynet_features_{}".format(configs.feature)
    )
    video_features = load_video_features(
        video_feature_path, max_position_length=configs.max_position_length
    )

    train_set = load_json(os.path.join(configs.save_dir, "train_set.json"))
    test_set = load_json(os.path.join(configs.save_dir, "test2_set.json"))
    num_train_batches = math.ceil(len(train_set) / configs.batch_size)

    if configs.num_train_steps is None:
        configs.num_train_steps = num_train_batches * configs.epochs
    if configs.char_size is None:
        configs.char_size = len(load_json(os.path.join(configs.save_dir, "char_dict.json")))

    log_dir = os.path.join(configs.home_dir, "event")
    model_dir = os.path.join(configs.home_dir, "model")
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # write configs to json file
    write_json(vars(configs), save_path=os.path.join(model_dir, "configs.json"), pretty=True)

    with tf.Graph().as_default() as graph:
        model = LPNet(configs, graph=graph)
        sess_config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess_config.gpu_options.allow_growth = True

        with tf.Session(config=sess_config) as sess:
            learning_rate = tf.train.exponential_decay(
                learning_rate=configs.init_lr,
                global_step=model.global_step,
                decay_steps=100000,
                decay_rate=0.9,
                staircase=True,
            )

            optimizer = tf.train.AdamOptimizer(
                learning_rate, beta1=0.9, beta2=0.999, name="AdamOptimizer"
            )
            # train_op = optimizer.minimize(model.my_loss, global_step=model.global_step)
            trainable_vars = tf.trainable_variables()
            freeze_bbox_var_list = [
                t for t in trainable_vars if not t.name.startswith("proposal_box")
            ]
            bbox_var_list = [t for t in trainable_vars if t.name.startswith("proposal_box")]
            train_op1 = optimizer.minimize(
                model.reg_loss, global_step=model.global_step, var_list=freeze_bbox_var_list
            )
            train_op2 = optimizer.minimize(
                model.my_loss, global_step=model.global_step, var_list=bbox_var_list
            )

            saver = tf.train.Saver(max_to_keep=5)
            writer = tf.summary.FileWriter(log_dir)
            sess.run(tf.global_variables_initializer())

            best_r1i7 = -1.0
            score_writer = open(
                os.path.join(model_dir, "eval_results.txt"), mode="w", encoding="utf-8"
            )
            l = 0
            r = 0
            o = 0
            for epoch in range(configs.epochs):
                for data in tqdm(
                    batch_iter(
                        train_set, video_features, configs.batch_size, configs.extend, True, True
                    ),
                    total=num_train_batches,
                    desc="Epoch %d / %d" % (epoch + 1, configs.epochs),
                ):

                    # run the model
                    feed_dict = get_feed_dict(data, model, configs.drop_rate)

                    _, _, loss, rloss, iloss, lloss, global_step = sess.run(
                        [
                            train_op1,
                            train_op2,
                            model.my_loss,
                            model.reg_loss,
                            model.iou_loss,
                            model.l1_loss,
                            model.global_step,
                        ],
                        feed_dict=feed_dict,
                    )

                    if global_step % configs.period == 0:
                        # write_tf_summary(writer, [("train/my_loss", loss)], global_step)
                        write_tf_summary(
                            writer,
                            [
                                ("train/my_loss", loss),
                                ("train/reg_loss", rloss),
                                ("train/iou_loss", iloss),
                                ("train/l1_loss", lloss),
                            ],
                            global_step,
                        )
                    # evaluate
                    # if global_step % configs.eval_period == 0 or global_step % num_train_batches == 0:
                    if (global_step / 2 + 1) % num_train_batches == 0:

                        r1i3, r1i5, r1i7, mi, value_pairs, score_str = eval_test(
                            sess=sess,
                            model=model,
                            dataset=test_set,
                            video_features=video_features,
                            configs=configs,
                            epoch=epoch + 1,
                            global_step=global_step,
                            name="test",
                        )

                        write_tf_summary(writer, value_pairs, global_step)
                        score_writer.write(score_str)
                        score_writer.flush()

                        # save the model according to the result of Rank@1, IoU=0.7
                        if r1i7 > best_r1i7:
                            best_r1i7 = r1i7
                            filename = os.path.join(model_dir, "model_{}.ckpt".format(global_step))
                            saver.save(sess, filename)

            score_writer.close()

elif configs.mode.lower() == "test":

    # load previous configs
    model_dir = os.path.join(configs.home_dir, "model")
    pre_configs = load_json(os.path.join(model_dir, "configs.json"))
    parser.set_defaults(**pre_configs)
    configs = parser.parse_args()

    # load video features
    video_feature_path = os.path.join(
        configs.root, "activitynet_features_{}".format(configs.feature)
    )
    video_features = load_video_features(
        video_feature_path, max_position_length=configs.max_position_length
    )

    # load test dataset
    test_set = load_json(os.path.join(configs.save_dir, "test2_set.json"))

    # restore model and evaluate
    with tf.Graph().as_default() as graph:
        model = LPNet(configs, graph=graph)
        sess_config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess_config.gpu_options.allow_growth = True

        with tf.Session(config=sess_config) as sess:
            saver = tf.train.Saver()
            sess.run(tf.global_variables_initializer())
            saver.restore(sess, tf.train.latest_checkpoint(model_dir))

            r1i3, r1i5, r1i7, mi, *_ = eval_test(
                sess,
                model,
                dataset=test_set,
                video_features=video_features,
                configs=configs,
                name="test",
            )

            print(
                "\n" + "\x1b[1;31m" + "Rank@1, IoU=0.3:\t{:.2f}".format(r1i3) + "\x1b[0m",
                flush=True,
            )
            print("\x1b[1;31m" + "Rank@1, IoU=0.5:\t{:.2f}".format(r1i5) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "Rank@1, IoU=0.7:\t{:.2f}".format(r1i7) + "\x1b[0m", flush=True)
            print(
                "\x1b[1;31m" + "{}:\t{:.2f}".format("mean IoU".ljust(15), mi[0]) + "\x1b[0m",
                flush=True,
            )

else:
    raise ValueError("Unknown mode {}!!!".format(configs.mode))
