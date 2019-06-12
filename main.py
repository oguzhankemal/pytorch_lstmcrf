
import argparse
import random
import numpy as np
from config.reader import Reader
from config import eval
from config.config import Config, ContextEmb
import time
from model.lstmcrf import NNCRF
import torch
import torch.optim as optim
import torch.nn as nn
from config.utils import lr_decay, simple_batching
from typing import List
from common.instance import Instance
from termcolor import colored
import os


def setSeed(opt, seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if opt.device.startswith("cuda"):
        print("using GPU...", torch.cuda.current_device())
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


def parse_arguments(parser):
    ###Training Hyperparameters
    parser.add_argument('--mode', type=str, default='train', choices=["train","test"], help="training mode or testing mode")
    parser.add_argument('--device', type=str, default="cpu", choices=['cpu','cuda:0','cuda:1','cuda:2'],help="GPU/CPU devices")
    parser.add_argument('--seed', type=int, default=42, help="random seed")
    parser.add_argument('--digit2zero', action="store_true", default=True, help="convert the number to 0, make it true is better")
    parser.add_argument('--dataset', type=str, default="conll2003")
    parser.add_argument('--embedding_file', type=str, default="/home/oguzhan/Marmara/Datasets/glove/glove.6B.100d.txt")
    # parser.add_argument('--embedding_file', type=str, default=None)
    parser.add_argument('--embedding_dim', type=int, default=100)
    parser.add_argument('--optimizer', type=str, default="sgd")
    parser.add_argument('--learning_rate', type=float, default=0.01) ##only for sgd now
    parser.add_argument('--momentum', type=float, default=0.0)
    parser.add_argument('--l2', type=float, default=1e-8)
    parser.add_argument('--lr_decay', type=float, default=0)
    parser.add_argument('--batch_size', type=int, default=10)
    parser.add_argument('--num_epochs', type=int, default=1)
    parser.add_argument('--train_num', type=int, default=-1)
    parser.add_argument('--dev_num', type=int, default=-1)
    parser.add_argument('--test_num', type=int, default=-1)

    ##model hyperparameter
    parser.add_argument('--hidden_dim', type=int, default=200, help="hidden size of the LSTM")

    ##NOTE: this dropout applies to many places
    parser.add_argument('--dropout', type=float, default=0.5, help="dropout for embedding")
    parser.add_argument('--use_char_rnn', type=int, default=1, choices=[0, 1], help="use character-level lstm, 0 or 1")
    parser.add_argument('--context_emb', type=str, default="none", choices=["none", "bert", "elmo", "flair"], help="contextual word embedding")




    args = parser.parse_args()
    for k in args.__dict__:
        print(k + ": " + str(args.__dict__[k]))
    return args


def get_optimizer(config: Config, model: nn.Module):
    params = model.parameters()
    if config.optimizer.lower() == "sgd":
        print(colored("Using SGD: lr is: {}, L2 regularization is: {}".format(config.learning_rate, config.l2), 'yellow'))
        return optim.SGD(params, lr=config.learning_rate, weight_decay=float(config.l2))
    elif config.optimizer.lower() == "adam":
        print(colored("Using Adam", 'yellow'))
        return optim.Adam(params)
    else:
        print("Illegal optimizer: {}".format(config.optimizer))
        exit(1)

def batching_list_instances(config: Config, insts:List[Instance]):
    train_num = len(insts)
    batch_size = config.batch_size
    total_batch = train_num // batch_size + 1 if train_num % batch_size != 0 else train_num // batch_size
    batched_data = []
    for batch_id in range(total_batch):
        one_batch_insts = insts[batch_id * batch_size:(batch_id + 1) * batch_size]
        batched_data.append(simple_batching(config, one_batch_insts))

    return batched_data

def learn_from_insts(config:Config, epoch: int, train_insts, dev_insts, test_insts):
    # train_insts: List[Instance], dev_insts: List[Instance], test_insts: List[Instance], batch_size: int = 1
    model = NNCRF(config)
    optimizer = get_optimizer(config, model)
    train_num = len(train_insts)
    print("number of instances: %d" % (train_num))
    print(colored("[Shuffled] Shuffle the training instance ids", "red"))
    random.shuffle(train_insts)



    batched_data = batching_list_instances(config, train_insts)
    dev_batches = batching_list_instances(config, dev_insts)
    test_batches = batching_list_instances(config, test_insts)

    best_dev = [-1, 0]
    best_test = [-1, 0]

    model_folder = "model_files"
    res_folder = "results"
    model_name = model_folder + "/lstm_{}_crf_{}_{}_dep_{}_elmo_{}_lr_{}.m".format(config.hidden_dim, config.dataset, config.train_num, config.context_emb.name, config.optimizer.lower(), config.learning_rate)
    res_name = res_folder + "/lstm_{}_crf_{}_{}_dep_{}_elmo_{}_lr_{}.results".format(config.hidden_dim, config.dataset, config.train_num, config.context_emb.name, config.optimizer.lower(), config.learning_rate)
    print("[Info] The model will be saved to: %s" % (model_name))
    if not os.path.exists(model_folder):
        os.makedirs(model_folder)
    if not os.path.exists(res_folder):
        os.makedirs(res_folder)

    for i in range(1, epoch + 1):
        epoch_loss = 0
        start_time = time.time()
        model.zero_grad()
        if config.optimizer.lower() == "sgd":
            optimizer = lr_decay(config, optimizer, i)
        for index in np.random.permutation(len(batched_data)):
        # for index in range(len(batched_data)):
            model.train()
            batch_word, batch_wordlen, batch_context_emb, batch_char, batch_charlen, batch_label = batched_data[index]
            loss = model.neg_log_obj(batch_word, batch_wordlen, batch_context_emb,batch_char, batch_charlen, batch_label)
            epoch_loss += loss.item()
            loss.backward()
            # # torch.nn.utils.clip_grad_norm_(model.parameters(), config.clip) ##clipping the gradient
            optimizer.step()
            model.zero_grad()

        end_time = time.time()
        print("Epoch %d: %.5f, Time is %.2fs" % (i, epoch_loss, end_time - start_time), flush=True)

        model.eval()
        dev_metrics = evaluate_model(config, model, dev_batches, "dev", dev_insts)
        test_metrics = evaluate_model(config, model, test_batches, "test", test_insts)
        if dev_metrics[2] > best_dev[0]:
            print("saving the best model...")
            best_dev[0] = dev_metrics[2]
            best_dev[1] = i
            best_test[0] = test_metrics[2]
            best_test[1] = i
            torch.save(model.state_dict(), model_name)
            write_results(res_name, test_insts)
        model.zero_grad()

    print("The best dev: %.2f" % (best_dev[0]))
    print("The corresponding test: %.2f" % (best_test[0]))
    print("Final testing.")
    model.load_state_dict(torch.load(model_name))
    model.eval()
    evaluate_model(config, model, test_batches, "test", test_insts)
    write_results(res_name, test_insts)



def evaluate_model(config:Config, model: NNCRF, batch_insts_ids, name:str, insts: List[Instance]):
    ## evaluation
    metrics = np.asarray([0, 0, 0], dtype=int)
    batch_id = 0
    batch_size = config.batch_size
    for batch in batch_insts_ids:
        one_batch_insts = insts[batch_id * batch_size:(batch_id + 1) * batch_size]
        sorted_batch_insts = sorted(one_batch_insts, key=lambda inst: len(inst.input.words), reverse=True)
        batch_max_scores, batch_max_ids = model.decode(batch)
        metrics += eval.evaluate_num(sorted_batch_insts, batch_max_ids, batch[-1], batch[1], config.idx2labels)
        batch_id += 1
    p, total_predict, total_entity = metrics[0], metrics[1], metrics[2]
    precision = p * 1.0 / total_predict * 100 if total_predict != 0 else 0
    recall = p * 1.0 / total_entity * 100 if total_entity != 0 else 0
    fscore = 2.0 * precision * recall / (precision + recall) if precision != 0 or recall != 0 else 0
    print("[%s set] Precision: %.2f, Recall: %.2f, F1: %.2f" % (name, precision, recall,fscore), flush=True)
    return [precision, recall, fscore]


def test_model(config: Config, test_insts):
    model_name = "model_files/lstm_{}_crf_{}_{}_dep_{}_elmo_{}_lr_{}.m".format(config.hidden_dim, config.dataset,
                                                                               config.train_num,
                                                                               config.context_emb.name,
                                                                               config.optimizer.lower(),
                                                                               config.learning_rate)
    res_name = "results/lstm_{}_crf_{}_{}_dep_{}_elmo_{}_lr_{}.results".format(config.hidden_dim, config.dataset,
                                                                               config.train_num,
                                                                               config.context_emb.name,
                                                                               config.optimizer.lower(),
                                                                               config.learning_rate)


    model = NNCRF(config)
    model.load_state_dict(torch.load(model_name))
    model.eval()
    test_batches = batching_list_instances(config, test_insts)
    evaluate_model(config, model, test_batches, "test", test_insts)
    write_results(res_name, test_insts)

def write_results(filename:str, insts):
    f = open(filename, 'w', encoding='utf-8')
    for inst in insts:
        for i in range(len(inst.input)):
            words = inst.input.words
            tags = inst.input.pos_tags
            output = inst.output
            prediction = inst.prediction
            assert  len(output) == len(prediction)
            f.write("{}\t{}\t{}\t{}\t{}\n".format(i, words[i], tags[i], output[i], prediction[i]))
        f.write("\n")
    f.close()






def main():
    TASKS = ['ner_german', 'ner']
    USE_DEV = True

    char_set = set()
    for task in TASKS:

        t = __import__(task)
        data_list = [t.TRAIN_DATA, t.DEV_DATA, t.TEST_DATA]
        char_index, _ = t.create_char_index(data_list)
        for k, v in char_index.items():
            char_set.add(k)
    char_index, char_cnt = {}, 0
    for char in char_set:
        char_index[char] = char_cnt
        char_cnt += 1

    for i, task in enumerate(TASKS):
        t = __import__(task)
        word_index, word_cnt = t.create_word_index([t.TRAIN_DATA, t.DEV_DATA, t.TEST_DATA])
        wx, y, m = t.read_data(t.TRAIN_DATA, word_index)
        if USE_DEV and task == 'ner':
            dev_wx, dev_y, dev_m = t.read_data(t.TEST_DATA, word_index)
            wx, y, m = np.vstack((wx, dev_wx)), np.vstack((y, dev_y)), np.vstack((m, dev_m))
        twx, ty, tm = t.read_data(t.DEV_DATA, word_index)
        x, cm = t.read_char_data(t.TRAIN_DATA, char_index)
        if USE_DEV and task == 'ner':
            dev_x, dev_cm = t.read_char_data(t.TEST_DATA, char_index)
            x, cm = np.vstack((x, dev_x)), np.vstack((cm, dev_cm))
        tx, tcm = t.read_char_data(t.DEV_DATA, char_index)
        if task == 'ner':
            list_prefix = t.read_list()
            gaze = t.read_list_data(t.TRAIN_DATA, list_prefix)
            tgaze = t.read_list_data(t.DEV_DATA, list_prefix)
            if USE_DEV:
                dev_gaze = t.read_list_data(t.TEST_DATA, list_prefix)
                gaze = np.vstack((gaze, dev_gaze))
        else:
            gaze, tgaze = None, None



    parser = argparse.ArgumentParser(description="LSTM CRF implementation")
    opt = parse_arguments(parser)
    conf = Config(opt)

    reader = Reader(conf.digit2zero)
    setSeed(opt, conf.seed)

    trains = reader.read_txt(conf.train_file, conf.train_num, True)
    devs = reader.read_txt(conf.dev_file, conf.dev_num, False)
    tests = reader.read_txt(conf.test_file, conf.test_num, False)
    trains_target = reader.read_txt(conf.train_target_file_file, conf.train_num, True)

    if conf.context_emb != ContextEmb.none:
        print('Loading the elmo vectors for all datasets.')
        conf.context_emb_size = reader.load_elmo_vec(conf.train_file + "."+conf.context_emb.name+".vec", trains)
        reader.load_elmo_vec(conf.dev_file  + "."+conf.context_emb.name+".vec", devs)
        reader.load_elmo_vec(conf.test_file + "."+conf.context_emb.name+".vec", tests)
    conf.use_iobes(trains)
    conf.use_iobes(devs)
    conf.use_iobes(tests)
    conf.build_label_idx(trains)
    conf.use_iobes(trains_target)
    conf.build_label_idx_target(trains_target)



    conf.build_word_idx(trains, devs, tests)
    conf.build_emb_table()

    ids_train = conf.map_insts_ids(trains)
    ids_dev = conf.map_insts_ids(devs)
    ids_test= conf.map_insts_ids(tests)


    print("num chars: " + str(conf.num_char))
    # print(str(config.char2idx))

    print("num words: " + str(len(conf.word2idx)))
    # print(config.word2idx)
    if opt.mode == "train":
        learn_from_insts(conf, conf.num_epochs, trains, devs, tests)
    else:
        ## Load the trained model.
        test_model(conf, tests)
        # pass

    print(opt.mode)

if __name__ == "__main__":
    main()