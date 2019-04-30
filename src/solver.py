import copy
import itertools
import math
import os
import json

import numpy as np
import torch
import torch.nn.functional as F
import torch.nn as nn

import matplotlib.pyplot as plt

from joblib import Parallel, delayed
from tensorboardX import SummaryWriter
from tqdm import tqdm

from asr import ASR
from dataset import load_dataset, prepare_x, prepare_y
from lm import LM
from postprocess import calc_acc, calc_err, draw_att

from text_autoencoder import TextAutoEncoder
from speech_autoencoder import SpeechAutoEncoder
from discriminator import Discriminator

# Additional Inference Timesteps to run during validation 
# (to calculate CER)
VAL_STEP = 30 
# steps for debugging info.
TRAIN_WER_STEP = 250
GRAD_CLIP = 5

class Solver:
    ''' Super class Solver for all kinds of tasks'''
    def __init__(self, config, paras, module_id):
        self.config = config
        self.paras = paras
        self.module_id = module_id
        
        # Logger Settings
        self.valid_step = config['solver']['eval_step']
        # Training details
        self.max_step = config['solver']['total_steps']


        if torch.cuda.is_available(): 
            self.device = torch.device('cuda') 
            self.paras.gpu = True
        else:
            self.device = torch.device('cpu')
            self.paras.gpu = False

        # e.g. ./runs/experiment_2
        self.logdir = os.path.join(self.paras.logdir, self.paras.name)
        self.log = SummaryWriter(self.logdir)

        # /result (by default)
        if not os.path.exists(paras.ckpdir):
            os.makedirs(paras.ckpdir)
        # /result/<name>/
        self.ckpdir = os.path.join(paras.ckpdir,self.paras.name)
        if not os.path.exists(self.ckpdir):
            os.makedirs(self.ckpdir)

        self.ckppath = os.path.join(self.ckpdir, self.module_id+'.cpt')
        self.tracker_path = os.path.join(self.ckpdir, 'tracker.json')

        self.step, self.best_val_loss = self.get_globals()

    def verbose(self, msg, progress=False):
        ''' Verbose function for print information to stdout'''
        end = '\r' if progress else '\n'
        if self.paras.verbose:
            print('[INFO]',msg, end=end)

    def log_scalar(self, key, val):
        self.log.add_scalar('{}_{}'.format(self.module_id, key), val, self.step)

    def log_text(self, key, val):
        self.log.add_text('{}_{}'.format(self.module_id, key), val, self.step)
    
    def log_image(self, key, val):
        self.log.add_image('{}_{}'.format(self.module_id, key), val, self.step)

    def get_globals(self):
        '''
        Returns (step, loss) where loss is the best loss
        that has been achived by the associated model and
        the global step when it was achieved. If the model
        is trained from scratch, we return (0, 10_000)
        '''
        if os.path.isfile(self.tracker_path):
            data = json.load(open(self.tracker_path, 'r'))
            if self.module_id in data:
                return data[self.module_id]['step'], data[self.module_id]['loss']
        return 0, 10_000

    def set_globals(self, step, loss):
        if not os.path.isfile(self.tracker_path):
            data = {}
        else:
            data = json.load(open(self.tracker_path, 'r'))
        data[self.module_id] = {}
        data[self.module_id]['step'] = step
        # actually is error in case of LAS, not loss
        data[self.module_id]['loss'] = loss

        json.dump(data, open(self.tracker_path, 'w'))

    def grad_clip(self, params, optim, grad_clip=GRAD_CLIP):
        grad_norm = nn.utils.clip_grad_norm_(params, GRAD_CLIP)
        if math.isnan(grad_norm):
            self.verbose('Error : grad norm is NaN @ step '+str(self.step))
        else:
            optim.step()


class ASRTrainer(Solver):
    ''' Handler for complete training progress'''
    def __init__(self, config, paras):
        super(ASRTrainer, self).__init__(config, paras , 'asr')
        
    def load_data(self):
        ''' Load date for training/validation'''
        
        (self.mapper, _ ,self.train_set) = load_dataset(
            self.config['solver']['train_index_path_byxlen'],
            batch_size=self.config['solver']['batch_size'],
            use_gpu=self.paras.gpu)
        
        (_, _, self.eval_set) = load_dataset(
            self.config['solver']['eval_index_path_byxlen'], 
            batch_size=self.config['solver']['eval_batch_size'],            
            use_gpu=self.paras.gpu)
        
    def set_model(self):
        ''' Setup ASR'''        
        self.seq_loss = nn.CrossEntropyLoss(ignore_index=0, 
            reduction='none').to(self.device)
        
        # Build attention end-to-end ASR
        self.asr_model = ASR(self.mapper.get_dim(),
            self.config['asr_model']).to(self.device)

        # check if a checkpoint has been written for this module
        if os.path.isfile(self.ckppath):
            # then we load a pretrained model
            self.asr_model.load_state_dict(torch.load(self.ckppath))

        if self.step > 0:
            self.verbose("Starting training at global step {} with best loss of {}"
                 .format(self.step, self.best_val_loss))
        
        # setup optimizer            
        self.optim = getattr(torch.optim,
            self.config['asr_model']['optimizer']['type'])
        self.optim = self.optim(self.asr_model.parameters(), 
            lr=self.config['asr_model']['optimizer']['learning_rate'], eps=1e-8)

    def exec(self):
        ''' Training End-to-end ASR system'''
        
        self.verbose('Training set total '+str(len(self.train_set))+' batches.')

        while self.step < self.max_step:
            for x, y in self.train_set:
                self.verbose('Global step: {}'.format(self.step), progress=True)

                (x, x_lens) = prepare_x(x, device=self.device)
                (y, y_lens) = prepare_y(y, device=self.device)

                state_len = x_lens

                '''
                TODO: Get to the bottom of this whole '<' skipping thing, how it is 
                tied into prepare_y and so on.

                The label was shorter by 1 (209 vs 210). Then I decreased ans_len 
                by 1 and now they are both as long as the label used to be (209)
                '''
                ans_len = max(y_lens) - 1

                # ASR forwarding 
                self.optim.zero_grad()
                _, prediction, _ = self.asr_model(x, ans_len, teacher=y, 
                    state_len=state_len)

                # Calculate loss function
                label = y[:,1:ans_len+1].contiguous()
        
                b,t,c = prediction.shape

                loss = self.seq_loss(prediction.view(b*t,c),label.view(-1))
                # Sum each uttr and devide by length
                loss = torch.sum(loss.view(b,t),dim=-1)/torch.sum(y!=0,dim=-1)\
                    .to(device = self.device, dtype=torch.float32)
                # Mean by batch
                loss = torch.mean(loss)                
                                
                # Backprop
                loss.backward()
                self.grad_clip(self.asr_model.parameters(), self.optim)
                
                # Logger
                self.log_scalar('train_loss', loss)
                self.log_scalar('train_acc', calc_acc(prediction,label))
                
                if self.step % TRAIN_WER_STEP == 0:
                    self.log_scalar('train_error', 
                        calc_err(prediction, label, mapper=self.mapper))

                # Validation
                if self.step % self.valid_step == 0 and self.step != 0:
                    self.optim.zero_grad()
                    self.valid()

                self.step += 1 
                if self.step > self.max_step: 
                    self.verbose('Stopping after reaching maximum training steps')
                    break
    
    def valid(self):
        '''Perform validation step'''
        self.asr_model.eval()
        
        # Init stats
        loss, att, acc, err = 0.0, 0.0, 0.0, 0.0
        val_len = 0    
        predictions, labels = [], []
        
        # Perform validation
        for b_idx,(x,y) in enumerate(self.eval_set):
            self.verbose('Validation step - {} ( {} / {} )'.format(
                self.step, b_idx, len(self.eval_set)), progress=True)
            
            (x, x_lens) = prepare_x(x, device=self.device)
            (y, y_lens) = prepare_y(y, device=self.device)

            # TODO: Same issue here as in self.exec()
            state_len = x_lens
            ans_len = max(y_lens) - 1

            # Forward
            state_len, prediction, att_map = self.asr_model(
                x, ans_len+VAL_STEP, state_len=state_len)

            # Compute attention loss & get decoding results
            label = y[:,1:ans_len+1].contiguous()
           
            seq_loss = self.seq_loss(prediction[:,:ans_len,:]
                .contiguous().view(-1,prediction.shape[-1]),label.view(-1))

            # Sum each uttr and devide by length
            seq_loss = torch.sum(seq_loss.view(x.shape[0],-1),dim=-1)\
                /torch.sum(y!=0,dim=-1).to(device=self.device, dtype=torch.float32)
            # Mean by batch
            seq_loss = torch.mean(seq_loss)
            
            loss += seq_loss.detach()*int(x.shape[0])
            
            mapped_prediction = [self.mapper.translate(p) for p in 
                np.argmax(prediction.cpu().detach(), axis=-1)]
            mapped_label = [self.mapper.translate(l) for l in label.cpu()]

            predictions.append(mapped_prediction)
            labels.append(mapped_label)
            
            acc += calc_acc(prediction,label)*int(x.shape[0])
            err += calc_err(prediction,label,mapper=self.mapper)*int(x.shape[0])
            
            val_len += int(x.shape[0])

        # Logger
        self.log_scalar('eval_loss', loss/val_len)
                     
        # Plot attention map to log for the last batch in the validation
        # dataset.
        val_hyp = [self.mapper.translate(p) for p in 
            np.argmax(prediction.cpu().detach(), axis=-1)]
        val_txt = [self.mapper.translate(l) for l in label.cpu()]
        val_attmaps = draw_att(att_map)
        
        # Record loss
        self.log_scalar('eval_error', err/val_len)
        self.log_scalar('eval_acc',  acc/val_len)
        
        for idx, attmap in enumerate(val_attmaps):
            #plt.imshow(attmap)
            #plt.show()
            self.log_image('eval_att_'+str(idx), attmap)
            self.log_text('eval_hyp_'+str(idx),val_hyp[idx])
            self.log_text('eval_txt_'+str(idx),val_txt[idx])
 
        # Save model by val er.
        if err/val_len  < self.best_val_loss:
            self.best_val_loss = err/val_len
            self.verbose('Best validation error : {:.4f} @ global step {}'
                .format(self.best_val_loss, self.step))

            self.set_globals(self.step, self.best_val_loss)
         
            torch.save(self.asr_model.state_dict(), self.ckppath)
            
            # Save hyps.
            with open(os.path.join(self.ckpdir, 'best_hyp.txt'), 'w') as f:
                for t1,t2 in zip(predictions, labels):
                    f.write(t1[0]+','+t2[0]+'\n')

        self.asr_model.train()

class SAETrainer(Solver):
    '''
    Train the Speech AutoEncoder.

    Parameters that this should train:
    * speech_autoenc.encoder (definetly)
    * speech_autoenc.decoder (definetly)
    * asr.listener              (likely)
    '''
    def __init__(self, config, paras):
        super(SAETrainer, self).__init__(config, paras, 'sae')
    
    def load_data(self):
        (self.mapper, _, self.train_set) = load_dataset(
            self.config['solver']['train_index_path_byxlen'], 
            batch_size=self.config['solver']['batch_size'], 
            use_gpu=self.paras.gpu)

        (_, _, self.eval_set) = load_dataset(
            self.config['solver']['eval_index_path_byxlen'], 
            batch_size=self.config['solver']['eval_batch_size'],
            use_gpu=self.paras.gpu)
    
    def set_model(self):
        self.asr_model = ASR(self.mapper.get_dim(),
            self.config['asr_model']).to(self.device)            

        p = self.config['speech_autoencoder']
        self.speech_autoenc = SpeechAutoEncoder(p['kernel_sizes'],
            p['num_filters'], p['pool_kernel_sizes'], 
            self.config['asr_model']['feature_dim'], 
            self.asr_model.get_listener().out_dim).to(self.device)

        if os.path.isfile(self.ckppath):
            # a pretrained text autoencoder exists
            self.verbose("Loading a pretrained Text Autoencoder")
            self.speech_autoenc.load_state_dict(torch.load(self.ckppath))

        if os.path.isfile(os.path.join(self.ckpdir, 'asr.cpt')):
            self.verbose("Loading a pretrained LAS model")
            self.asr_model.load_state_dict(
                torch.load(os.path.join(self.ckpdir, 'asr.cpt')))

        # set loss metrics and optimizer

        '''
        The optimizer will optimize:
        * The whole SpeechAutoencoder
        * The listener of the ASR model
        '''        
        self.optim = getattr(torch.optim, p['optimizer']['type'])
        self.optim = self.optim(
            list(self.speech_autoenc.parameters()) + \
            list(self.asr_model.encoder.parameters()), 
            lr=p['optimizer']['learning_rate'], eps=1e-8)

        self.loss_metric = nn.SmoothL1Loss(reduction='none')

    def exec(self):
        self.verbose('Training set total '+str(len(self.train_set))+' batches.')

        for x, y in self.train_set:
            self.verbose('Global step: {}'.format(self.step), progress=True)

            x, x_lens = prepare_x(x)
            self.optim.zero_grad()

            enc_out = self.speech_autoenc(self.asr_model, x, x_lens)
            # pad the encoder output UP to the maximum batch time frames and 
            # pad x DOWN to the same number of frames
            batch_t = max(x_lens)
            x = x[:, :batch_t, :]
            enc_final = torch.zeros([enc_out.shape[0], batch_t, enc_out.shape[2]])
            enc_final[:, :enc_out.shape[1], :] = enc_out
        
            loss = self.loss_metric(enc_final, x)

            # Divide each by length of x and sum, then take the average over the
            # batch
            loss = torch.sum(loss.view(loss.shape[0], -1)) / torch.Tensor(x_lens)
            loss = torch.mean(loss)
            loss.backward()
            self.grad_clip(self.speech_autoenc.parameters(), self.optim)

            self.log_scalar('train_loss', loss)

            if self.step % self.valid_step == 0 and self.step != 0:
                self.optim.zero_grad()
                self.valid()
            
            self.step += 1
            
            if self.step > self.max_step:
                self.verbose('Stopping after reaching maximum training steps')
                break
    
    def valid(self):
        self.speech_autoenc.eval()
        self.asr_model.eval()

        avg_loss = 0.0
        n_batches = 0
        for b_idx, (x, y) in enumerate(self.eval_set):
            self.verbose('Validation step - {} ( {} / {} )'.format(
                self.step, b_idx, len(self.eval_set)), progress=True)
            
            x, x_lens = prepare_x(x)

            enc_out = self.speech_autoenc(self.asr_model, x, x_lens)
            # pad the encoder output UP to the maximum batch time frames and 
            # pad x DOWN to the same number of frames
            batch_t = max(x_lens)
            x = x[:, :batch_t, :]
            enc_final = torch.zeros([enc_out.shape[0], batch_t, enc_out.shape[2]])
            enc_final[:, :enc_out.shape[1], :] = enc_out
        
            loss = self.loss_metric(enc_final, x)

            # Divide each by length of x and sum, then take the average over the
            # batch
            loss = torch.sum(loss.view(loss.shape[0], -1)) / torch.Tensor(x_lens)
            loss = torch.mean(loss)
           
            avg_loss += loss.detach()
            n_batches += 1
        
        avg_loss /=  n_batches
        avg_loss = avg_loss.item()      
        if avg_loss < self.best_val_loss:
            # then we save the model
            self.best_val_loss = avg_loss
            self.set_globals(self.step, self.best_val_loss)

            self.verbose('Best validation loss : {:.4f} @ global step {}'
                .format(self.best_val_loss, self.step))

            torch.save(self.speech_autoenc.state_dict(), self.ckppath)

            # I should probably evaluate the ASR model as well, but we should
            # be save saving it as well.
            torch.save(self.asr_model.state_dict(), os.path.join(self.ckpdir, 'asr.cpt'))

        self.speech_autoenc.train()
        self.asr_model.train() 
                   
class TAETrainer(Solver):
    '''
    Train the Text AutoEncoder
    '''
    def __init__(self, config, paras):
        super(TAETrainer, self).__init__(config, paras, 'tae')

    def load_data(self):
        '''
        These are loaded with text only and noise, meaning dataloader
        will return (clean_y, noise_y) for both the training and validation
        sets
        '''
        (self.mapper, self.dataset, self.train_set) = load_dataset(
            self.config['solver']['train_index_path_byylen'], 
            batch_size=self.config['solver']['batch_size'], 
            use_gpu=self.paras.gpu, text_only=True, 
            drop_rate=self.config['text_autoencoder']['drop_rate'])

        (_, _, self.eval_set) = load_dataset(
            self.config['solver']['eval_index_path_byylen'], 
            batch_size=self.config['solver']['eval_batch_size'],
            use_gpu=self.paras.gpu, text_only=True,
            drop_rate=self.config['text_autoencoder']['drop_rate'])

    def set_model(self):
        self.asr_model = ASR(self.mapper.get_dim(),
            self.config['asr_model']).to(self.device)            

        p = self.config['text_autoencoder']

        self.text_autoenc = TextAutoEncoder(self.mapper.get_dim(), 
            emb_dim = p['emb_dim'], state_size=p['state_size'],
            num_layers=p['num_layers']).to(self.device)
        
        if os.path.isfile(self.ckppath):
            # a pretrained text autoencoder exists
            self.verbose("Loading a pretrained Text Autoencoder")
            self.text_autoenc.load_state_dict(torch.load(self.ckppath))

        if os.path.isfile(os.path.join(self.ckpdir, 'asr.cpt')):
            self.verbose("Loading a pretrained LAS model")
            self.asr_model.load_state_dict(
                torch.load(os.path.join(self.ckpdir, 'asr.cpt')))

        '''
        The optimizer will optimize:
        * The whole textautoencoder
        * The ASR character embedding
        * The whole ASR attention module
        * The whole ASR speller module
        * The ASR char_trans linear layer
        '''

        self.optim = getattr(torch.optim, p['optimizer']['type'])
        self.optim = self.optim( 
                list(self.text_autoenc.parameters()) + \
                list(self.asr_model.embed.parameters()) + \
                list(self.asr_model.attention.parameters()) + \
                list(self.asr_model.decoder.parameters()) + \
                list(self.asr_model.char_trans.parameters()),
                lr=p['optimizer']['learning_rate'], eps=1e-8)

        self.loss_metric = torch.nn.CrossEntropyLoss(ignore_index=0, 
            reduction='none').to(self.device)

    def exec(self):
        self.verbose('Training set total '+str(len(self.train_set))+' batches.')

        for b_idx, (y, y_noise) in enumerate(self.train_set):
            self.verbose('Global step: {}'.format(self.step), progress=True)

            y, y_lens = prepare_y(y)
            y_max_len = max(y_lens)

            y_noise, y_noise_lens = prepare_y(y_noise)
            y_noise_max_lens = max(y_noise_lens)
            
            self.optim.zero_grad()

            # decode steps == longest target
            decode_step = y_max_len 
            noise_lens, enc_out = self.text_autoenc(self.asr_model, y, y_noise, 
                decode_step, noise_lens=y_noise_lens)
            
            b,t,c = enc_out.shape
            loss = self.loss_metric(enc_out.view(b*t,c), y.view(-1))
            # Sum each uttr and devide by length
            loss = torch.sum(loss.view(b,t),dim=-1)/torch.sum(y!=0,dim=-1)\
                .to(device = torch.device('cpu'), dtype=torch.float32)
            
            # Mean by batch
            loss = torch.mean(loss)
            loss.backward()
            self.grad_clip(self.text_autoenc.parameters(), self.optim)


            self.log_scalar('train_loss', loss)
            
            if self.step % self.valid_step == 0 and self.step != 0:
                self.optim.zero_grad()
                self.valid()
            
            self.step += 1
            
            if self.step > self.max_step:
                self.verbose('Stopping after reaching maximum training steps')
                break

    def valid(self):
        self.text_autoenc.eval()
        self.asr_model.eval()

        avg_loss = 0.0
        n_batches = 0
        for b_idx, (y, y_noise) in enumerate(self.eval_set):
            self.verbose('Validation step - {} ( {} / {} )'.format(
                self.step, b_idx, len(self.eval_set)), progress=True)
            
            y, y_lens = prepare_y(y)
            y_max_len = max(y_lens)

            y_noise, y_noise_lens = prepare_y(y_noise)
            y_noise_max_lens = max(y_noise_lens)
            
            # decode steps == longest target
            decode_step = y_max_len 
            noise_lens, enc_out = self.text_autoenc(self.asr_model, y, y_noise, 
                decode_step, noise_lens=y_noise_lens)
            
            b,t,c = enc_out.shape
            loss = self.loss_metric(enc_out.view(b*t,c), y.view(-1))
            # Sum each uttr and devide by length
            loss = torch.sum(loss.view(b,t),dim=-1)/torch.sum(y!=0,dim=-1)\
                .to(device = torch.device('cpu'), dtype=torch.float32)
            
            # Mean by batch
            loss = torch.mean(loss)
            self.log_scalar('eval_loss', loss)

            avg_loss += loss.detach()
            n_batches += 1
        
        avg_loss /=  n_batches
        avg_loss = avg_loss.item()      
        if avg_loss < self.best_val_loss:
            # then we save the model
            self.best_val_loss = avg_loss
            self.set_globals(self.step, self.best_val_loss)

            self.verbose('Best validation loss : {:.4f} @ global step {}'
                .format(self.best_val_loss, self.step))

            torch.save(self.text_autoenc.state_dict(), self.ckppath)

            # I should probably evaluate the ASR model as well, but we should
            # be save saving it as well.
            torch.save(self.asr_model.state_dict(), os.path.join(self.ckpdir, 'asr.cpt'))

        self.text_autoenc.train()
        self.asr_model.train() 

class AdvTrainer(Solver):
    '''
    Do adversarial training on both the Discriminator and
    the Generator (Listener) using the Text encoder as a 
    data source
    '''
    def __init__(self, config, paras):
        super(AdvTrainer, self).__init__(config, paras, 'adv')

    def load_data(self):
        (self.mapper, self.dataset, self.train_set) = load_dataset(
            self.config['solver']['train_index_path_byxlen'], 
            batch_size=self.config['solver']['batch_size'], 
            use_gpu=self.paras.gpu)

        (_, _, self.eval_set) = load_dataset(
            self.config['solver']['eval_index_path_byxlen'], 
            batch_size=self.config['solver']['eval_batch_size'],
            use_gpu=self.paras.gpu)
        
    def set_model(self):

        p = self.config['text_autoencoder']
        d = self.config['discriminator']

        self.asr_model = ASR(self.mapper.get_dim(),
            self.config['asr_model']).to(self.device)            

        self.text_autoenc = TextAutoEncoder(self.mapper.get_dim(), 
            emb_dim = p['emb_dim'], state_size=p['state_size'],
            num_layers=p['num_layers']).to(self.device)

        self.discriminator = Discriminator(self.asr_model.encoder.get_outdim(), 
            d['hidden_dim']).to(self.device)

        # load models if they exist
        if os.path.isfile(self.ckppath):
            # a pretrained text autoencoder exists
            self.verbose("Loading a pretrained Discriminator")
            self.discriminator.load_state_dict(torch.load(self.ckppath))

        if os.path.isfile(os.path.join(self.ckpdir, 'asr.cpt')):
            self.verbose("Loading a pretrained LAS model")
            self.asr_model.load_state_dict(
                torch.load(os.path.join(self.ckpdir, 'asr.cpt')))

        if os.path.isfile(os.path.join(self.ckpdir, 'tae.cpt')):
            self.verbose("Loading a pretrained SAE model")
            self.text_autoenc.load_state_dict(
                torch.load(os.path.join(self.ckpdir, 'tae.cpt')))

        self.data_distribution = self.text_autoenc.encoder
        
        self.G_optim = getattr(torch.optim, d['G_optimizer']['type'])
        self.G_optim = self.G_optim(self.asr_model.encoder.parameters(), 
            lr=d['G_optimizer']['learning_rate'], eps=1e-8)

        self.D_optim = getattr(torch.optim, d['D_optimizer']['type'])
        self.D_optim = self.D_optim(self.discriminator.parameters(), 
            lr=d['D_optimizer']['learning_rate'], eps=1e-8)

        self.loss_metric = torch.nn.BCELoss().to(self.device)

    def exec(self):
        '''
        Adversarial training:
        * (G)enerator: Listener (from LAS)
        * (D)iscriminator: This discriminator module
        * Data distribution: The text encoder
        '''
        for b_idx, (x, y) in enumerate(self.train_set):
            self.verbose('Global step - {} ( {} / {} )'.format(
                self.step, b_idx, len(self.train_set)), progress=True)     

            x, x_lens = prepare_x(x)    
            y, y_lens = prepare_y(y)
            
            batch_size = x.shape[0]
            '''
            DISCRIMINATOR TRAINING
            maximize log(D(x)) + log(1 - D(G(z)))
            '''
            self.discriminator.zero_grad()

            '''Discriminator real data training'''
            real_data = self.data_distribution(y) # [bs, seq, 512]
            D_out = self.discriminator(real_data)
            real_labels = torch.ones(batch_size, real_data.shape[1]) \
                - self.config['discriminator']['label_smoothing']
            D_realloss = self.loss_metric(D_out.squeeze(dim=2), real_labels)
            D_realloss.backward()

            '''Discriminator fake data training'''
            fake_data, _ = self.asr_model.encoder(x, x_lens)
            # Note: fake_data.detach() is used here to avoid backpropping
            # through the generator. (see grad_pointers.gp_6 for details)
            D_out = self.discriminator(fake_data.detach())
            fake_labels = torch.zeros(batch_size, fake_data.shape[1])
            D_fakeloss = self.loss_metric(D_out.squeeze(dim=2), fake_labels)
            D_fakeloss.backward()
            
            # update the parameters and collect total loss
            D_totalloss = D_realloss + D_fakeloss

            self.grad_clip(self.discriminator.parameters(), self.D_optim)

            self.log_scalar('discrim_real_loss_train', D_realloss)
            self.log_scalar('discrim_fake_loss_train', D_fakeloss)
            self.log_scalar('discrim_loss_train', D_totalloss)


            '''
            GENERATOR TRAINING 
            maximize log(D(G(z)))
            '''
            self.asr_model.encoder.zero_grad()

            # fake labels for the listener are the true labels for the
            # discriminator 
            fake_labels = torch.ones(batch_size, fake_data.shape[1])
            
            '''
            we cant call .detach() here, to avoid gradient calculations
            on the discriminator, since then we would lose the history needed
            to update the generator.

            x -> G -> fake_data -> D -> D_out -> Calculate loss
            
            vs.

            x -> G -> fake_data -> fake_data.detach()
            [    LOST HISTORY    ]      | -> D -> D_out -> Calculate loss
            '''
            D_out = self.discriminator(fake_data)

            G_loss = self.loss_metric(D_out.squeeze(dim=2), fake_labels)
            G_loss.backward()
            self.grad_clip(self.asr_model.encoder.parameters(), self.G_optim)

            self.log_scalar('gen_loss_train', G_loss)


            if self.step % self.valid_step == 0 and self.step != 0:
                self.G_optim.zero_grad()
                self.D_optim.zero_grad()
                self.validate_discriminator()
            
            self.step += 1
            
            if self.step > self.max_step:
                self.verbose('Stopping after reaching maximum training steps')
                break
        
    def validate_discriminator(self):
        self.discriminator.eval()

        avg_loss = 0.0
        n_batches = 0
        for b_idx, (x, y) in enumerate(self.eval_set):
            self.verbose('Validation step - {} ( {} / {} )'.format(
                self.step, b_idx, len(self.eval_set)), progress=True)
            
            x, x_lens = prepare_x(x)    
            y, y_lens = prepare_y(y)
            
            batch_size = x.shape[0]

            '''Discriminator real data training'''
            real_data = self.data_distribution(y) # [bs, seq, 512]
            D_out = self.discriminator(real_data)
            real_labels = torch.ones(batch_size, real_data.shape[1])
            
            D_realloss = self.loss_metric(D_out.squeeze(dim=2), real_labels)

            '''Discriminator fake data training'''
            fake_data, _ = self.asr_model.encoder(x, x_lens)
            # Note: fake_data.detach() is used here to avoid backpropping
            # through the generator. (see grad_pointers.gp_6 for details)
            D_out = self.discriminator(fake_data.detach())
            fake_labels = torch.zeros(batch_size, fake_data.shape[1])
            D_fakeloss = self.loss_metric(D_out.squeeze(dim=2), fake_labels)
            
            # update the parameters and collect total loss
            D_totalloss = D_realloss + D_fakeloss

            self.log_scalar('discrim_real_loss_eval', D_realloss)
            self.log_scalar('discrim_fake_loss_eval', D_fakeloss)
            self.log_scalar('discrim_loss_eval', D_totalloss)
            avg_loss += D_totalloss.detach()
            n_batches += 1
        
        avg_loss /=  n_batches
        avg_loss = avg_loss.item()      
        if avg_loss < self.best_val_loss:
            # then we save the model
            self.best_val_loss = avg_loss
            self.set_globals(self.step, self.best_val_loss)

            self.verbose('Best validation loss : {:.4f} @ global step {}'
                .format(self.best_val_loss, self.step))

            torch.save(self.discriminator.state_dict(), self.ckppath)

            # I should probably evaluate the ASR model as well, but we should
            # be save saving it as well.
            #torch.save(self.asr_model.state_dict(), os.path.join(self.ckpdir, 'asr.cpt'))

        self.discriminator.train()

class LMTrainer(Solver):
    ''' Trainer for RNN-LM only'''
    def __init__(self, config, paras):
        super(LMTrainer, self).__init__(config, paras, 'rnn_lm')

    def load_data(self):
        ''' Load training / evaluation sets '''
        (self.mapper, _, self.train_set) = load_dataset(
            self.config['solver']['train_index_path_byylen'], 
            batch_size=self.config['solver']['batch_size'], 
            use_gpu=self.paras.gpu, text_only=True)

        (_, _, self.eval_set) = load_dataset(
            self.config['solver']['eval_index_path_byylen'], 
            batch_size=self.config['solver']['eval_batch_size'],
            use_gpu=self.paras.gpu, text_only=True)

    def set_model(self):
        ''' Setup RNNLM'''
        self.verbose('Init RNNLM model.')

        self.rnnlm = LM(out_dim=self.mapper.get_dim(), 
            **self.config['rnn_lm']['model_para'])

        if os.path.isfile(self.ckppath):
            # then we load a pretrained model
            self.rnnlm.load_state_dict(torch.load(self.ckppath))

        self.rnnlm = self.rnnlm.to(self.device)

        # optimizer
        self.optim = getattr(torch.optim,
            self.config['rnn_lm']['optimizer']['type'])
        self.optim = self.optim(self.rnnlm.parameters(), 
            lr=self.config['rnn_lm']['optimizer']['learning_rate'],eps=1e-8)

    def exec(self):
        ''' Training RNN-LM'''
        self.verbose('RNN-LM Training set total '+str(len(self.train_set))+' batches.')

        while self.step < self.max_step:
            for y in self.train_set:
                self.verbose('Global step: {}'.format(self.step), progress=True)

                (y, y_lens) = prepare_y(y, device=self.device)
                # BUG: we remove 1 from y_lens (see prepare_y thing)
                y_lens = [l-1 for l in y_lens]                

                self.optim.zero_grad()
                
                _, prob = self.rnnlm(y[:,:-1], y_lens)
                
                loss = F.cross_entropy(prob.view(-1,prob.shape[-1]), 
                    y[:,1:].contiguous().view(-1), ignore_index=0)
                loss.backward()
                self.optim.step()

                # logger
                ppx = torch.exp(loss.cpu()).item()
                self.log_scalar('train_perplexity', ppx)

                # Next step
                self.step += 1

                if self.step % self.valid_step ==0:
                    self.valid()

                if self.step > self.max_step:
                    break

    def valid(self):
        self.rnnlm.eval()

        print_loss = 0.0
        eval_size = 0 

        for cur_b,y in enumerate(self.eval_set):
            self.verbose('Validation step - {} ( {} / {} )'.format(
                self.step, cur_b, len(self.eval_set)), progress=True)
            
            (y, y_lens) = prepare_y(y, device=self.device)
            # same issue here as in self.exec()
            y_lens = [l-1 for l in y_lens]

            _, prob = self.rnnlm(y[:,:-1], y_lens)
            loss = F.cross_entropy(prob.view(-1,prob.shape[-1]), 
                y[:,1:].contiguous().view(-1), ignore_index=0)
            
            print_loss += loss.clone().detach() * y.shape[0]
            eval_size += y.shape[0]

        print_loss /= eval_size
        eval_ppx = torch.exp(print_loss).cpu().item()
        self.log_scalar('eval_perplexity', eval_ppx)
        
        # Store model with the best perplexity
        if eval_ppx < self.best_val_loss:
            self.best_val_loss  = eval_ppx
            self.verbose('Best validation ppx : {:.4f} @ step {}'
                .format(self.best_val_loss, self.step))

            self.set_globals(self.step, self.best_val_loss)

            torch.save(self.rnnlm.state_dict(), self.ckppath)

        self.rnnlm.train()
