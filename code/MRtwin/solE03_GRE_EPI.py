"""
Created on Tue Jan 29 14:38:26 2019
@author: mzaiss

"""
experiment_id = 'solE03_GRE_EPI'
sequence_class = "epi"
experiment_description = """
2 D imaging
"""
excercise = """
This is  currenty a FLASH sequence like  A10
B01.1. remove all rf_events exept for the very first one, make this 90°, remove all y gradients, remove all rf_phases
        Now, there should be only an echo in the very first repetition. 
B01.2. Think of a way to get back again some magnetization in the second repetition without using an additional rf event, but a gradient.
B01.3. If the last task was successful, do the same trick for all repetitions. Decrease the even_times until you see an echo in each repetition.
B01.4. Try to cover the full k-space again, by adding phase encoding-gradients.
B01.5. If you image looks weird, you might have forgotten somthing. Analyse again carfully step B01.3.
        Also  a single pixel pahntom can be helpful, use this in line 130 to overwrite:
        real_phantom_resized[:,:,:3]*=0
        real_phantom_resized[10,10,:3]=1 
        Look at sim04_reverse.py to find a solution.
B01.6. The image can still show a ghost. This is a so called N/2 ghost. See http://mriquestions.com/nyquist-n2-ghosts.html for more info.
        However, here in the simulation it should be possible to remove it competely. Carefully analyse you ADC trajectory using seq_pic
B01.7. Now you have an echo-planar imaging sequence, EPI, one of the fastes MRI sequences. While its speed is amazing, it also has some drawbacks: 
        Try increasing the B0 inhomogeneity. 
        Try to prolong the event times. What do you observe.
B01.8.  
"""
#%%
#matplotlib.pyplot.close(fig=None)
#%%
import os, sys
import numpy as np
import scipy
import scipy.io
from  scipy import ndimage
import torch
import cv2
import matplotlib.pyplot as plt
from torch import optim
import core.spins
import core.scanner
import core.nnreco
import core.target_seq_holder
import warnings
import matplotlib.cbook
warnings.filterwarnings("ignore",category=matplotlib.cbook.mplDeprecation)

from importlib import reload
reload(core.scanner)

double_precision = False
do_scanner_query = False

use_gpu = 1
gpu_dev = 0

if sys.platform != 'linux':
    use_gpu = 0
    gpu_dev = 0
print(experiment_id)    
print('use_gpu = ' +str(use_gpu)) 

# NRMSE error function
def e(gt,x):
    return 100*np.linalg.norm((gt-x).ravel())/np.linalg.norm(gt.ravel())
    
# torch to numpy
def tonumpy(x):
    return x.detach().cpu().numpy()

# get magnitude image
def magimg(x):
    return np.sqrt(np.sum(np.abs(x)**2,2))

def phaseimg(x):
    return np.angle(1j*x[:,:,1]+x[:,:,0])

def magimg_torch(x):
  return torch.sqrt(torch.sum(torch.abs(x)**2,1))

def tomag_torch(x):
    return torch.sqrt(torch.sum(torch.abs(x)**2,-1))

# device setter
def setdevice(x):
    if double_precision:
        x = x.double()
    else:
        x = x.float()
    if use_gpu:
        x = x.cuda(gpu_dev)    
    return x 

#############################################################################
## S0: define image and simulation settings::: #####################################
sz = np.array([48,48])                      # image size
extraMeas = 1                               # number of measurmenets/ separate scans
NRep = extraMeas*sz[1]                      # number of total repetitions
szread=sz[1]
NEvnt = szread + 5 + 2                               # number of events F/R/P
NSpins = 16**2                               # number of spin sims in each voxel
NCoils = 1                                  # number of receive coil elements
noise_std = 0*100*1e-3                        # additive Gaussian noise std
kill_transverse = False                     #
import time; today_datestr = time.strftime('%y%m%d')
NVox = sz[0]*szread

#############################################################################
## S1: Init spin system and phantom::: #####################################
# initialize scanned object
spins = core.spins.SpinSystem(sz,NVox,NSpins,use_gpu+gpu_dev,double_precision=double_precision)

cutoff = 1e-12
#real_phantom = scipy.io.loadmat('../../data/phantom2D.mat')['phantom_2D']
real_phantom = scipy.io.loadmat('../../data/numerical_brain_cropped.mat')['cropped_brain']

real_phantom_resized = np.zeros((sz[0],sz[1],5), dtype=np.float32)
for i in range(5):
    t = cv2.resize(real_phantom[:,:,i], dsize=(sz[0],sz[1]), interpolation=cv2.INTER_CUBIC)
    if i == 0:
        t[t < 0] = 0
    elif i == 1 or i == 2:
        t[t < cutoff] = cutoff        
    real_phantom_resized[:,:,i] = t
    
#real_phantom_resized[:,:,:3]*=0
#real_phantom_resized[10,10,:3]=1 

real_phantom_resized[:,:,1] *= 1 # Tweak T1
real_phantom_resized[:,:,2] *= 1 # Tweak T2
real_phantom_resized[:,:,3] *= 2 # Tweak dB0
real_phantom_resized[:,:,4] *= 1 # Tweak rB1

spins.set_system(real_phantom_resized)

if 0:
    plt.figure("""phantom""")
    param=['PD','T1','T2','dB0','rB1']
    for i in range(5):
        plt.subplot(151+i), plt.title(param[i])
        ax=plt.imshow(real_phantom_resized[:,:,i].transpose(), interpolation='none')
        fig = plt.gcf()
        fig.colorbar(ax) 
    fig.set_size_inches(18, 3)
    plt.show()
   
#begin nspins with R2* = 1/T2*
R2star = 30.0
omega = np.linspace(0,1,NSpins) - 0.5   # cutoff might bee needed for opt.
omega = np.expand_dims(omega[:],1).repeat(NVox, axis=1)
omega*=0.99 # cutoff large freqs
omega = R2star * np.tan ( np.pi  * omega)
spins.omega = torch.from_numpy(omega.reshape([NSpins,NVox])).float()
spins.omega = setdevice(spins.omega)
## end of S1: Init spin system and phantom ::: #####################################


#############################################################################
## S2: Init scanner system ::: #####################################
scanner = core.scanner.Scanner_fast(sz,NVox,NSpins,NRep,NEvnt,NCoils,noise_std,use_gpu+gpu_dev,double_precision=double_precision,do_voxel_rand_ramp_distr=False)

B1plus = torch.zeros((scanner.NCoils,1,scanner.NVox,1,1), dtype=torch.float32)
B1plus[:,0,:,0,0] = torch.from_numpy(real_phantom_resized[:,:,4].reshape([scanner.NCoils, scanner.NVox]))
B1plus[B1plus == 0] = 1    # set b1+ to one, where we dont have phantom measurements
B1plus[:] = 1
scanner.B1plus = setdevice(B1plus)

#############################################################################
## S3: MR sequence definition ::: #####################################
# begin sequence definition
# allow for extra events (pulses, relaxation and spoiling) in the first five and last two events (after last readout event)
adc_mask = torch.from_numpy(np.ones((NEvnt,1))).float()
adc_mask[:5]  = 0
adc_mask[-2:] = 0
scanner.set_adc_mask(adc_mask=setdevice(adc_mask))

# RF events: rf_event and phases
rf_event = torch.zeros((NEvnt,NRep,2), dtype=torch.float32)
rf_event[3,0,0] = 90*np.pi/180  # 90deg excitation now for every rep

rf_event = setdevice(rf_event)
scanner.init_flip_tensor_holder()    
scanner.set_flip_tensor_withB1plus(rf_event)
# rotate ADC according to excitation phase
rfsign = ((rf_event[3,:,0]) < 0).float()
scanner.set_ADC_rot_tensor(-rf_event[3,:,1]+ np.pi/2 + np.pi*rfsign) #GRE/FID specific

# event timing vector 
event_time = torch.from_numpy(0.008*10*1e-3*np.ones((NEvnt,NRep))).float()
event_time[3,0] = 2e-3
event_time[4,0] = 0.3*1e-3
event_time = setdevice(event_time)

# gradient-driver precession
# Cartesian encoding
gradm_event = torch.zeros((NEvnt,NRep,2), dtype=torch.float32)
gradm_event[4,0,0] = -0.5*szread
gradm_event[4,0,1] =  -0.5*NRep 
#gradm_event[-1,::2,1] = 1.0
gradm_event[4,1::2,1] = -1.0
gradm_event[4,2::2,1] = +1.0
gradm_event[5:-2,::2,1] = 1.0
gradm_event[5:-2,1::2,1] = -1.0
gradm_event[4,1:,0] = 1 #phase blib

gradm_event = setdevice(gradm_event)

scanner.init_gradient_tensor_holder()
scanner.set_gradient_precession_tensor(gradm_event,sequence_class)  # refocusing=False for GRE/FID, adjust for higher echoes
## end S3: MR sequence definition ::: #####################################


#############################################################################
## S4: MR simulation forward process ::: #####################################
scanner.init_signal()
#%%
targetSeq = core.target_seq_holder.TargetSequenceHolder(rf_event,event_time,gradm_event,scanner,spins,scanner.signal)
targetSeq.export_to_pulseq(experiment_id,today_datestr,sequence_class,plot_seq=True,single_folder=True)

scanner.get_signal_from_real_system(experiment_id,today_datestr,single_folder=True)
       
#%%
#scanner.forward_fast(spins, event_time)

#targetSeq = core.target_seq_holder.TargetSequenceHolder(rf_event,event_time,gradm_event,scanner,spins,scanner.signal)
targetSeq.print_seq_pic(True,plotsize=[12,9])


fig=plt.figure("""seq and image"""); 
plt.subplot(411); plt.ylabel('RF, time, ADC'); plt.title("Total acquisition time ={:.2} s".format(tonumpy(torch.sum(event_time))))
plt.plot(np.tile(tonumpy(adc_mask),NRep).flatten('F'),'.',label='ADC')
plt.plot(tonumpy(event_time).flatten('F'),'.',label='time')
plt.plot(tonumpy(rf_event[:,:,0]).flatten('F'),label='RF')
major_ticks = np.arange(0, NEvnt*NRep, NEvnt) # this adds ticks at the correct position szread
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.subplot(412); plt.ylabel('gradients')
plt.plot(tonumpy(gradm_event[:,:,0]).flatten('F'),label='gx')
plt.plot(tonumpy(gradm_event[:,:,1]).flatten('F'),label='gy')
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.subplot(413); plt.ylabel('signal')
plt.plot(tonumpy(scanner.signal[0,:,:,0,0]).flatten('F'),label='real')
plt.plot(tonumpy(scanner.signal[0,:,:,1,0]).flatten('F'),label='imag')
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.show()
  
#%% ############################################################################
## S5: MR reconstruction of signal ::: #####################################

spectrum = tonumpy(scanner.signal[0,adc_mask.flatten()!=0,:,:2,0].clone()) 
spectrum = spectrum[:,:,0]+spectrum[:,:,1]*1j # get all ADC signals as complex numpy array
#inverse_perm = np.arange(len(permvec))[np.argsort(permvec)]
#spectrum=spectrum[:,inverse_perm]
#spectrum[:,permvec]=spectrum
spectrum[:,1::2]=(spectrum[::-1,1::2])

plt.subplot(413); plt.ylabel('signal')
plt.plot(np.real(spectrum).flatten('F'),label='real')
plt.plot(spectrum.imag.flatten('F'),label='imag')

kspace=spectrum
spectrum = np.roll(spectrum,szread//2,axis=0)
spectrum = np.roll(spectrum,NRep//2,axis=1)
space = np.zeros_like(spectrum)
space = np.fft.ifft2(spectrum)
space = np.roll(space,szread//2-1,axis=0)
space = np.roll(space,NRep//2-1,axis=1)
space = np.flip(space,(0,1))

   
plt.subplot(4,6,19)
plt.imshow(real_phantom_resized[:,:,0].transpose(), interpolation='none'); plt.xlabel('PD')
plt.subplot(4,6,20)
plt.imshow(real_phantom_resized[:,:,3].transpose(), interpolation='none'); plt.xlabel('dB0')

plt.subplot(4,6,22)
plt.imshow(np.abs(kspace).transpose(), interpolation='none'); plt.xlabel('kspace')
plt.subplot(4,6,23)
plt.imshow(np.abs(space).transpose(), interpolation='none',aspect = sz[0]/szread); plt.xlabel('mag_img')
plt.subplot(4,6,24)
mask=(np.abs(space)>0.2*np.max(np.abs(space)))
plt.imshow(np.angle(space).transpose()*mask.transpose(), interpolation='none',aspect = sz[0]/szread); plt.xlabel('phase_img')
plt.show()                     
