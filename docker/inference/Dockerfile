FROM seunglab/chunkflow:latest

COPY pytorch-emvision $HOME/workspace/chunkflow/docker/inference/pytorch-emvision/ 
COPY pytorch-model $HOME/workspace/chunkflow/docker/inference/pytorch-model/

RUN echo "export PYTHONPATH=$HOME/workspace/chunkflow/docker/inference/pytorch-model:$HOME/workspace/chunkflow/docker/inference/pytorch-emvision:\$PYTHONPATH" >> $HOME/.bashrc;

WORKDIR $HOME/workspace/chunkflow/bin

