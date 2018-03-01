import argparse
import logging
import os
import pickle
import gc

import math
import time
import tensorboard
from tensorboard import summary

import torch
from torch import nn, optim
from torch.nn.utils import clip_grad_norm
from torch.optim import lr_scheduler
from torch.nn.functional import softmax
from torch.utils.data import DataLoader
from collections import defaultdict

from snli.model import SNLIBinaryModel, SNLIAttModel
from snli.utils.dataset import SNLIDataset
from utils.glove import load_glove
from utils.helper import * 
import conf
from IPython import embed


def train(args):
    with open(args.train_data, 'rb') as f:
        train_dataset: SNLIDataset = pickle.load(f)
    with open(args.valid_data, 'rb') as f:
        valid_dataset: SNLIDataset = pickle.load(f)
    with open(args.test_data, 'rb') as f:
        test_dataset: SNLIDataset = pickle.load(f)

    train_loader = DataLoader(dataset=train_dataset, batch_size=args.batch_size,
                              shuffle=True, num_workers=2,
                              collate_fn=train_dataset.collate,
                              pin_memory=True)
    valid_loader = DataLoader(dataset=valid_dataset, batch_size=args.batch_size,
                              shuffle=False, num_workers=2,
                              collate_fn=valid_dataset.collate,
                              pin_memory=True)
    test_loader = DataLoader(dataset=test_dataset, batch_size=args.batch_size,
                              shuffle=False, num_workers=2,
                              collate_fn=valid_dataset.collate,
                              pin_memory=True)
    word_vocab = train_dataset.word_vocab
    label_vocab = train_dataset.label_vocab

    if args.use_important_words and len(args.important_words) > 0:
        logging.info('Set _id_tf of important_words to 0')
        logging.info('words: %s' % ','.join(args.important_words))
        for word in args.important_words:
            word_vocab._id_tf[word_vocab.word_to_id(word)] = 0            

    if args.model_type == 'binary':
        model = SNLIBinaryModel(num_classes=len(label_vocab), num_words=len(word_vocab),
                      word_dim=args.word_dim, hidden_dim=args.hidden_dim,
                      clf_hidden_dim=args.clf_hidden_dim,
                      clf_num_layers=args.clf_num_layers,
                      use_leaf_rnn=args.leaf_rnn,
                      use_batchnorm=args.batchnorm,
                      intra_attention=args.intra_attention,
                      dropout_prob=args.dropout,
                      bidirectional=args.bidirectional,
                      weighted_by_interval_length=args.weighted,
                      weighted_base=args.weighted_base,
                      weighted_update=args.weighted_update,
                      cell_type=args.cell_type)
    elif args.model_type == 'att':
        model = SNLIAttModel(vocab=word_vocab,
                      num_classes=len(label_vocab), num_words=len(word_vocab),
                      word_dim=args.word_dim, hidden_dim=args.hidden_dim,
                      clf_hidden_dim=args.clf_hidden_dim,
                      clf_num_layers=args.clf_num_layers,
                      use_leaf_rnn=args.leaf_rnn,
                      use_batchnorm=args.batchnorm,
                      dropout_prob=args.dropout,
                      bidirectional=args.bidirectional,
                      cell_type=args.cell_type,
                      att_type=args.att_type,
                      sample_num=args.sample_num)
    logging.info(model)
    if args.glove:
        logging.info('Loading GloVe pretrained vectors...')
        glove_weight = load_glove(
            path=args.glove, vocab=word_vocab,
            init_weight=model.word_embedding.weight.data.numpy())
        glove_weight[word_vocab.pad_id] = 0
        model.word_embedding.weight.data.set_(torch.FloatTensor(glove_weight))
    if args.fix_word_embedding:
        logging.info('Will not update word embeddings')
        model.word_embedding.weight.requires_grad = False
    if args.gpu > -1:
        logging.info(f'Using GPU {args.gpu}')
        model.cuda(args.gpu)
    if args.optimizer == 'adam':
        optimizer_class = optim.Adam
    elif args.optimizer == 'adagrad':
        optimizer_class = optim.Adagrad
    elif args.optimizer == 'adadelta':
        optimizer_class = optim.Adadelta
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = optimizer_class(params=params, lr=args.lr, weight_decay=args.l2reg)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer=optimizer, mode='max', factor=0.5, patience=10, verbose=True)
    criterion = nn.CrossEntropyLoss()


    train_summary_writer = tensorboard.FileWriter(
        logdir=os.path.join(args.save_dir, 'log', 'train'), flush_secs=10)
    valid_summary_writer = tensorboard.FileWriter(
        logdir=os.path.join(args.save_dir, 'log', 'valid'), flush_secs=10)

    def run_iter(batch, is_training):
        model.train(is_training)
        pre = wrap_with_variable(batch['pre'], volatile=not is_training,
                                 gpu=args.gpu)
        hyp = wrap_with_variable(batch['hyp'], volatile=not is_training,
                                 gpu=args.gpu)
        pre_length = wrap_with_variable(
            batch['pre_length'], volatile=not is_training, gpu=args.gpu)
        hyp_length = wrap_with_variable(
            batch['hyp_length'], volatile=not is_training, gpu=args.gpu)
        label = wrap_with_variable(batch['label'], volatile=not is_training,
                                   gpu=args.gpu)
        if is_training:
            logits, supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length)
            label_pred = logits.max(1)[1]
            accuracy = torch.eq(label, label_pred).float().mean()
            loss = criterion(input=logits, target=label)
            optimizer.zero_grad()
            loss.backward()
            clip_grad_norm(parameters=params, max_norm=5)
            optimizer.step()
            return loss, accuracy
        else:
            if args.model_type == 'binary':
                logits, supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length)
                pre_select_masks, hyp_select_masks = supplements['pre_select_masks'], supplements['hyp_select_masks']
                label_pred = logits.max(1)[1]
                accuracy = torch.eq(label, label_pred).float().mean()
                num_correct = torch.eq(label, label_pred).long().sum()
                loss = criterion(input=logits, target=label)
                depth_cumsum_1, word_cumsum_1 = parse_tree_avg_depth(pre_length, pre_select_masks)
                depth_cumsum_2, word_cumsum_2 = parse_tree_avg_depth(hyp_length, hyp_select_masks)
                return num_correct, (depth_cumsum_1+depth_cumsum_2), (word_cumsum_1+word_cumsum_2)
            elif args.model_type == 'att':
                logits, supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length, display=True)
                pre_tree, hyp_tree = supplements['pre_tree'], supplements['hyp_tree']
                label_pred = logits.max(1)[1]
                accuracy = torch.eq(label, label_pred).float().mean()
                num_correct = torch.eq(label, label_pred).long().sum()
                wrong_mask = torch.ne(label, label_pred).data.cpu().numpy()
                wrong_tree_pairs = [(pre_tree[i], hyp_tree[i]) for i in range(wrong_mask.shape[0]) if wrong_mask[i]==1]
                loss = criterion(input=logits, target=label)
                return num_correct, pre_tree, hyp_tree, wrong_tree_pairs

    num_train_batches = len(train_loader)
    validate_every = num_train_batches // 10
    best_vaild_accuacy = 0
    iter_count = 0
    model_path = None
    for epoch_num in range(args.max_epoch):
        logging.info(f'Epoch {epoch_num}: start')
        for batch_iter, train_batch in enumerate(train_loader):
            train_loss, train_accuracy = run_iter(
                batch=train_batch, is_training=True)
            iter_count += 1
            add_scalar_summary(
                summary_writer=train_summary_writer,
                name='loss', value=train_loss, step=iter_count)
            add_scalar_summary(
                summary_writer=train_summary_writer,
                name='accuracy', value=train_accuracy, step=iter_count)

            if (batch_iter + 1) % validate_every == 0:
                valid_accuracy_sum = depth_cumsum = word_cumsum = 0
                # Validation phase should be based on model_type !
                if args.model_type == 'binary':
                    for valid_batch in valid_loader:
                        valid_correct, depth_cumsum_, word_cumsum_ = run_iter(
                            batch=valid_batch, is_training=False)
                        valid_accuracy_sum += unwrap_scalar_variable(valid_correct)
                        depth_cumsum += depth_cumsum_
                        word_cumsum += word_cumsum_
                    valid_accuracy = valid_accuracy_sum / len(valid_dataset) 
                    valid_depth = depth_cumsum / word_cumsum
                elif args.model_type == 'att':
                    wrong_tree_pairs = []
                    for valid_batch in valid_loader:
                        valid_correct, pre_tree, hyp_tree, _wrong_tree_pairs = run_iter(
                            batch=valid_batch, is_training=False)
                        valid_accuracy_sum += unwrap_scalar_variable(valid_correct)
                        wrong_tree_pairs += _wrong_tree_pairs
                    valid_accuracy = valid_accuracy_sum / len(valid_dataset) 
                    valid_depth = -1
                    # print some sample trees
                    logging.info(' ***** sample wrong tree pairs ***** ')
                    display = '\n'
                    for p, h in wrong_tree_pairs[:10]:
                        display += '%s\n%s\n-----\n' % (p, h)
                    logging.info(display + ' ***********************  ')
                scheduler.step(valid_accuracy)
                add_scalar_summary(
                    summary_writer=valid_summary_writer,
                    name='accuracy', value=valid_accuracy, step=iter_count)
                progress = epoch_num + batch_iter/num_train_batches
                logging.info(f'Epoch {progress:.2f}: '
                             f'valid accuracy = {valid_accuracy:.4f}, '
                             f'depth = {valid_depth:.2f}')
                if args.model_type == 'binary' and args.weighted_update:
                    logging.info(f'weighted_base: {model.encoder.weighted_base.data[0]:.4f}')
                if valid_accuracy > best_vaild_accuacy:
                    #############################
                    # test performance
                    test_accuracy_sum = 0
                    for test_batch in test_loader:
                        test_correct, _, _ = run_iter(batch=test_batch, is_training=False)
                        test_accuracy_sum += unwrap_scalar_variable(test_correct)
                    test_accuracy = test_accuracy_sum / len(test_dataset)
                    ############################
                    best_vaild_accuacy = valid_accuracy
                    model_filename = (f'model-{progress:.2f}'
                            f'-{valid_accuracy:.4f}'
                            f'-{test_accuracy:.4f}.pkl')
                    model_path = os.path.join(args.save_dir, model_filename)
                    torch.save(model.state_dict(), model_path)
                    print(f'Saved the new best model to {model_path}')
    # log all wrong predictions
    if args.model_type == 'att':
        logging.info(' ***** all wrong tree pairs in validation set ***** ')
        display = '\n'
        for p, h in wrong_tree_pairs:
            display += '%s\n%s\n-----\n' % (p, h)
        logging.info(display + ' ***********************  ')




def train_withsampleRL(args):
    with open(args.train_data, 'rb') as f:
        train_dataset: SNLIDataset = pickle.load(f)
    with open(args.valid_data, 'rb') as f:
        valid_dataset: SNLIDataset = pickle.load(f)
    with open(args.test_data, 'rb') as f:
        test_dataset: SNLIDataset = pickle.load(f)

    train_loader = DataLoader(dataset=train_dataset, batch_size=args.batch_size,
                              shuffle=True, num_workers=2,
                              collate_fn=train_dataset.collate,
                              pin_memory=True)
    valid_loader = DataLoader(dataset=valid_dataset, batch_size=args.batch_size,
                              shuffle=False, num_workers=2,
                              collate_fn=valid_dataset.collate,
                              pin_memory=True)
    test_loader = DataLoader(dataset=test_dataset, batch_size=args.batch_size,
                              shuffle=False, num_workers=2,
                              collate_fn=valid_dataset.collate,
                              pin_memory=True)
    word_vocab = train_dataset.word_vocab
    label_vocab = train_dataset.label_vocab

    if args.use_important_words and len(args.important_words) > 0:
        logging.info('Set _id_tf of important_words to 0')
        logging.info('words: %s' % ','.join(args.important_words))
        for word in args.important_words:
            word_vocab._id_tf[word_vocab.word_to_id(word)] = 0

    model = SNLIAttModel(vocab=word_vocab,
                      num_classes=len(label_vocab), num_words=len(word_vocab),
                      word_dim=args.word_dim, hidden_dim=args.hidden_dim,
                      clf_hidden_dim=args.clf_hidden_dim,
                      clf_num_layers=args.clf_num_layers,
                      use_leaf_rnn=args.leaf_rnn,
                      use_batchnorm=args.batchnorm,
                      dropout_prob=args.dropout,
                      bidirectional=args.bidirectional,
                      cell_type=args.cell_type,
                      att_type=args.att_type,
                      sample_num=args.sample_num,
                      rich_state=args.rich_state,
                      rank_init=args.rank_init,
                      rank_input=args.rank_input,
                      rank_detach=(args.rank_detach==1),
                      rank_tanh=args.rank_tanh)
    logging.info(model)
    if args.glove:
        logging.info('Loading GloVe pretrained vectors...')
        glove_weight = load_glove(
            path=args.glove, vocab=word_vocab,
            init_weight=model.word_embedding.weight.data.numpy())
        glove_weight[word_vocab.pad_id] = 0
        model.word_embedding.weight.data.set_(torch.FloatTensor(glove_weight))
    if args.fix_word_embedding:
        logging.info('Will not update word embeddings')
        model.word_embedding.weight.requires_grad = False
    if args.gpu > -1:
        logging.info(f'Using GPU {args.gpu}')
        model.cuda(args.gpu)
    if args.optimizer == 'adam':
        optimizer_class = optim.Adam
    elif args.optimizer == 'adagrad':
        optimizer_class = optim.Adagrad
    elif args.optimizer == 'adadelta':
        optimizer_class = optim.Adadelta
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = optimizer_class(params=params, lr=args.lr, weight_decay=args.l2reg)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer=optimizer, mode='max', factor=0.5, patience=10, verbose=True)   
    
    criterion = nn.CrossEntropyLoss()


    train_summary_writer = tensorboard.FileWriter(
        logdir=os.path.join(args.save_dir, 'log', 'train'), flush_secs=10)
    valid_summary_writer = tensorboard.FileWriter(
        logdir=os.path.join(args.save_dir, 'log', 'valid'), flush_secs=10)

    def run_iter(batch, is_training):
        model.train(is_training)
        pre = wrap_with_variable(batch['pre'], volatile=not is_training,
                                 gpu=args.gpu)
        hyp = wrap_with_variable(batch['hyp'], volatile=not is_training,
                                 gpu=args.gpu)
        pre_length = wrap_with_variable(
            batch['pre_length'], volatile=not is_training, gpu=args.gpu)
        hyp_length = wrap_with_variable(
            batch['hyp_length'], volatile=not is_training, gpu=args.gpu)
        label = wrap_with_variable(batch['label'], volatile=not is_training,
                                   gpu=args.gpu)
        if is_training:
            logits, supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length)
            sample_logits, pre_probs, hyp_probs, hyp_sample_trees = supplements['sample_logits'], supplements['pre_probs'], supplements['hyp_probs'], supplements['hyp_sample_trees']
            #######################
            # supervise training for greedy tree structure, one tree per sentence
            label_pred = logits.max(1)[1]
            sv_accuracy = torch.eq(label, label_pred).float().mean()
            sv_loss = criterion(input=logits, target=label)
            #######################
            # rl training for sampled trees, sample_num trees per sentence
            sample_label_pred = sample_logits.max(1)[1]
            sample_label_gt = label.unsqueeze(1).expand(-1, args.sample_num).contiguous().view(-1)
            # expand gt for args.sample_num times, because each sentence has sample_num samples
            rl_rewards = torch.eq(sample_label_gt, sample_label_pred).float().detach() * 2 - 1
            rl_loss = 0
            
            # average of word
            final_probs = defaultdict(list)
            for i in range(len(label)):
                cand_rewards = rl_rewards[i*args.sample_num: (i+1)*args.sample_num]
                for j in range(args.sample_num):
                    k = i * args.sample_num + j
                    for w in pre_probs[k]:
                        final_probs[w] += [p*rl_rewards[k] for p in pre_probs[k][w]]
                    for w in hyp_probs[k]:
                        final_probs[w] += [p*rl_rewards[k] for p in hyp_probs[k][w]]
            for w in final_probs:
                rl_loss += - sum(final_probs[w]) / len(final_probs[w])
            if len(final_probs) > 0:
                rl_loss /= len(final_probs)

            rl_loss *= args.rl_weight
            #######################
            total_loss = sv_loss + rl_loss
            optimizer.zero_grad()
            total_loss.backward()
            clip_grad_norm(parameters=params, max_norm=5)
            optimizer.step()

            if conf.debug:
                sample_num = args.sample_num
                info = ''
                info += '\n ############################################################################ \n'
                p = softmax(logits, dim=1)
                scores = model.encoder.scores
                sample_p = softmax(sample_logits, dim=1)
                logits, new_supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length, display=True)
                new_p = softmax(logits, dim=1)
                new_scores = model.encoder.scores
                for i in range(7):
                    info += ', '.join(map(lambda x: f'{x:.2f}', list(scores[i].squeeze().data)))+'\n'
                    info += '%s\t%.2f, %.2f, %.2f\t%d\n' % (supplements['hyp_tree'][i], unwrap_scalar_variable(p[i][0]), unwrap_scalar_variable(p[i][1]), unwrap_scalar_variable(p[i][2]), unwrap_scalar_variable(label[i]))
                    for j in range(i*sample_num, (i+1)*sample_num):
                        info += '%s\t%.2f, %.2f, %.2f\t%.2f\t%d\n' % (hyp_sample_trees[j], unwrap_scalar_variable(sample_p[j][0]), unwrap_scalar_variable(sample_p[j][1]), unwrap_scalar_variable(sample_p[j][2]), unwrap_scalar_variable(rl_rewards[j]), unwrap_scalar_variable(sample_label_gt[j]))
                    info += '%s\t%.2f, %.2f, %.2f\t%d\n' % (new_supplements['hyp_tree'][i], unwrap_scalar_variable(new_p[i][0]), unwrap_scalar_variable(new_p[i][1]), unwrap_scalar_variable(new_p[i][2]), unwrap_scalar_variable(label[i]))
                    info += ', '.join(map(lambda x: f'{x:.2f}', list(new_scores[i].squeeze().data)))+'\n'
                    info += ' -------------------------------------------- \n'
                info += ' >>>\n >>>\n'
                wrong_mask = torch.ne(label, label_pred).data.cpu().numpy()
                wrong_i = [i for i in range(wrong_mask.shape[0]) if wrong_mask[i]==1]
                for i in wrong_i[:3]:
                    info += ', '.join(map(lambda x: f'{x:.2f}', list(scores[i].squeeze().data)))+'\n'
                    info += '%s\t%.2f, %.2f, %.2f\t%d\n' % (supplements['hyp_tree'][i], unwrap_scalar_variable(p[i][0]), unwrap_scalar_variable(p[i][1]), unwrap_scalar_variable(p[i][2]), unwrap_scalar_variable(label[i]))
                    for j in range(i*sample_num, (i+1)*sample_num):
                        info += '%s\t%.2f, %.2f, %.2f\t%.2f\t%d\n' % (hyp_sample_trees[j], unwrap_scalar_variable(sample_p[j][0]), unwrap_scalar_variable(sample_p[j][1]), unwrap_scalar_variable(sample_p[j][2]), unwrap_scalar_variable(rl_rewards[j]), unwrap_scalar_variable(sample_label_gt[j]))
                    info += '%s\t%.2f, %.2f, %.2f\t%d\n' % (new_supplements['hyp_tree'][i], unwrap_scalar_variable(new_p[i][0]), unwrap_scalar_variable(new_p[i][1]), unwrap_scalar_variable(new_p[i][2]), unwrap_scalar_variable(label[i]))
                    info += ', '.join(map(lambda x: f'{x:.2f}', list(new_scores[i].squeeze().data)))+'\n'
                    info += ' -------------------------------------------- \n'
                logging.info(info)

            return sv_loss, rl_loss, sv_accuracy
        else:
            logits, supplements = model(pre=pre, pre_length=pre_length, hyp=hyp, hyp_length=hyp_length, display=True)
            pre_tree, hyp_tree, sample_logits = supplements['pre_tree'], supplements['hyp_tree'], supplements['sample_logits']
            label_pred = logits.max(1)[1]
            accuracy = torch.eq(label, label_pred).float().mean()
            num_correct = torch.eq(label, label_pred).long().sum()
            wrong_mask = torch.ne(label, label_pred).data.cpu().numpy()
            wrong_tree_pairs = [(pre_tree[i], hyp_tree[i]) for i in range(wrong_mask.shape[0]) if wrong_mask[i]==1]
            loss = criterion(input=logits, target=label)
            return num_correct, pre_tree, hyp_tree, wrong_tree_pairs

    num_train_batches = len(train_loader)
    validate_every = num_train_batches // 10
    best_vaild_accuacy = 0
    iter_count = 0
    model_path = None
    initial_rl_weight = args.rl_weight
    tic = time.time()

    for epoch_num in range(args.max_epoch):
        logging.info(f'Epoch {epoch_num}: start')
        for batch_iter, train_batch in enumerate(train_loader):
            ###############
            # args.rl_weight = initial_rl_weight / (1 + 100 * train_loader.epoch / args.max_epoch)
            # train and summary
            train_sv_loss, train_rl_loss, train_accuracy = run_iter(batch=train_batch, is_training=True)
            iter_count += 1
            add_scalar_summary(
                summary_writer=train_summary_writer,
                name='sv_loss', value=train_sv_loss, step=iter_count)
            add_scalar_summary(
                summary_writer=train_summary_writer,
                name='rl_loss', value=train_rl_loss, step=iter_count)
            add_scalar_summary(
                summary_writer=train_summary_writer,
                name='accuracy', value=train_accuracy, step=iter_count)
            for name, p in model.named_parameters():
                if p.requires_grad and p.grad is not None and 'rank' in name:
                    add_histo_summary(
                        summary_writer=train_summary_writer,
                        name='value/'+name, value=p, step=iter_count)
                    add_histo_summary(
                        summary_writer=train_summary_writer,
                        name='grad/'+name, value=p.grad, step=iter_count)
            ###############

            if (batch_iter + 1) % (num_train_batches // 100) == 0:
                progress = epoch_num + batch_iter/num_train_batches
                tac = (time.time() - tic) / 60
                print(f'   {tac:.2f} minutes\tprogress: {progress:.2f}')
            
            ###############
            # validate and logging
            if (batch_iter + 1) % validate_every == 0:
                valid_accuracy_sum = 0
                ###############################
                wrong_tree_pairs = []
                for valid_batch in valid_loader:
                    valid_correct, pre_tree, hyp_tree, _wrong_tree_pairs = run_iter(
                        batch=valid_batch, is_training=False)
                    valid_accuracy_sum += unwrap_scalar_variable(valid_correct)
                    wrong_tree_pairs += _wrong_tree_pairs
                valid_accuracy = valid_accuracy_sum / len(valid_dataset) 
                # print some sample trees
                '''logging.info(' ***** sample wrong tree pairs ***** ')
                display = '\n'
                for p, h in wrong_tree_pairs[:5]:
                    display += '%s\n%s\n-----\n' % (p, h)
                logging.info(display + ' ***********************  ')'''
                
                scheduler.step(valid_accuracy)
                add_scalar_summary(
                    summary_writer=valid_summary_writer,
                    name='accuracy', value=valid_accuracy, step=iter_count)
                progress = epoch_num + batch_iter/num_train_batches
                logging.info(f'Epoch {progress:.2f}: '
                             f'valid accuracy = {valid_accuracy:.4f}, ')
                if valid_accuracy > best_vaild_accuacy:
                    #############################
                    # test performance
                    test_accuracy_sum = 0
                    for test_batch in test_loader:
                        test_correct, _, _, _ = run_iter(batch=test_batch, is_training=False)
                        test_accuracy_sum += unwrap_scalar_variable(test_correct)
                    test_accuracy = test_accuracy_sum / len(test_dataset)
                    ############################
                    best_vaild_accuacy = valid_accuracy
                    model_filename = (f'model-{progress:.2f}'
                            f'-{valid_accuracy:.4f}'
                            f'-{test_accuracy:.4f}.pkl')
                    model_path = os.path.join(args.save_dir, model_filename)
                    torch.save(model.state_dict(), model_path)
                    print(f'Saved the new best model to {model_path}')
    # log all wrong predictions
    logging.info(' ***** all wrong tree pairs in validation set ***** ')
    display = '\n'
    for p, h in wrong_tree_pairs:
        display += '%s\n%s\n-----\n' % (p, h)
    logging.info(display + ' ***********************  ')



def main():
    parser = argparse.ArgumentParser(fromfile_prefix_chars='@')
    parser.add_argument('--train-data', default='./data/snli_train.pickle')
    parser.add_argument('--valid-data', default='./data/snli_dev.pickle')
    parser.add_argument('--test-data', default='./data/snli_test.pickle')
    parser.add_argument('--glove', default='/data/share/glove.840B/glove.840B.300d.txt')
    parser.add_argument('--save-dir', required=True)
    parser.add_argument('--gpu', default=0, type=int)
    parser.add_argument('--cell_type', default='treelstm', choices=['treelstm', 'Nary', 'TriPad'])
    parser.add_argument('--model_type', default='binary', choices=['binary', 'att'])
    parser.add_argument('--att_type', default='corpus', choices=['corpus', 'rank0', 'rank1', 'rank2'], help='Used only when model_type==att')
    parser.add_argument('--sample_num', default=2, type=int)
    parser.add_argument('--rich-state', default=False, action='store_true')
    parser.add_argument('--rank_init', default='kaiming', choices=['normal', 'kaiming'])
    parser.add_argument('--rank_input', default='word', choices=['word', 'h'])
    parser.add_argument('--rank_detach', default=1, choices=[0, 1], type=int, help='1 means detach, 0 means no detach')
    parser.add_argument('--rank_tanh', action='store_true')
    
    
    parser.add_argument('--rl_weight', default=0.1, type=float)
    parser.add_argument('--use_important_words', default=False, action='store_true')
    parser.add_argument('--important_words', default=['no','No','NO','not','Not','NOT','isn\'t','aren\'t','hasn\'t','haven\'t','can\'t'])
    parser.add_argument('--word-dim', default=300, type=int)
    parser.add_argument('--hidden-dim', default=300, type=int)
    parser.add_argument('--clf-hidden-dim', default=1024, type=int)
    parser.add_argument('--clf-num-layers', default=1, type=int)
    parser.add_argument('--leaf-rnn', default=True, action='store_true')
    parser.add_argument('--bidirectional', default=False, action='store_true')
    parser.add_argument('--intra-attention', default=False, action='store_true')
    parser.add_argument('--batchnorm', default=True, action='store_true')
    parser.add_argument('--dropout', default=0.1, type=float)
    parser.add_argument('--fix-word-embedding', action='store_true')
    parser.add_argument('--batch-size', default=128, type=int)
    parser.add_argument('--max-epoch', default=10, type=int)
    parser.add_argument('--lr', default=0.001, type=float)
    parser.add_argument('--optimizer', default='adam')
    parser.add_argument('--l2reg', default=1e-5, type=float)

    parser.add_argument('--weighted', default=False, action='store_true')
    parser.add_argument('--weighted_base', type=float, default=2)
    parser.add_argument('--weighted_update', default=False, action='store_true')
    args = parser.parse_args()

    #######################################
    # a simple log file, the same content as stdout
    if not os.path.exists(args.save_dir):
        os.mkdir(args.save_dir)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
    logFormatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
    rootLogger = logging.getLogger()
    fileHandler = logging.FileHandler(os.path.join(args.save_dir, 'stdout.log'))
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)
    ########################################
    
    for k, v in vars(args).items():
        logging.info(k+':'+str(v))

    if args.att_type == 'corpus':
        train(args)
    else:
        train_withsampleRL(args)


if __name__ == '__main__':
    main()
