
import os
import sys
import numpy as np
import torch


class Trainer:
    r"""Class to train a model.

    This contains an internal training loop which takes care of validation and can be
    extended with custom functionality using hooks.

    Args:
       model_path (str): path to the model directory.
       model (torch.Module): model to be trained.
       loss_fn (callable): training loss function.
       optimizer (torch.optim.optimizer.Optimizer): training optimizer.
       train_loader (torch.utils.data.DataLoader): data loader for training set.
       validation_loader (torch.utils.data.DataLoader): data loader for validation set.
       keep_n_checkpoints (int, optional): number of saved checkpoints.
       checkpoint_interval (int, optional): intervals after which checkpoints is saved.
       hooks (list, optional): hooks to customize training process.
       loss_is_normalized (bool, optional): if True, the loss per data point will be
           reported. Otherwise, the accumulated loss is reported.

   """

    def __init__(
            self,
            model_path,
            model,
            loss_fn,
            optimizer,
            train_loader,
            validation_loader,
            keep_n_checkpoints=3,
            checkpoint_interval=10,
            validation_interval=1,
            hooks=[],
            loss_is_normalized=True,
            n_acc_steps=1,
            remember=10,
            ensembleModel=False,
    ):
        self.model_path = model_path
        self.checkpoint_path = os.path.join(self.model_path, "checkpoints")
        self.best_model = os.path.join(self.model_path, "best_model")
        self.train_loader = train_loader
        self.validation_loader = validation_loader
        self.validation_interval = validation_interval
        self.keep_n_checkpoints = keep_n_checkpoints
        self.hooks = hooks
        self.loss_is_normalized = loss_is_normalized
        self.n_acc_steps = n_acc_steps
        self.remember = remember
        self.ensembleModel = ensembleModel

        self._model = model
        self._stop = False
        self.checkpoint_interval = checkpoint_interval

        self.loss_fn = loss_fn
        self.optimizer = optimizer

        if os.path.exists(self.checkpoint_path):
            self.restore_checkpoint()
        else:
            os.makedirs(self.checkpoint_path)
            self.epoch = 0
            self.epoch_losses = []
            self.step = 0
            self.best_loss = float("inf")
            self.best_losses = []
            self.store_checkpoint()

    def _check_is_parallel(self):
        return True if isinstance(self._model, torch.nn.DataParallel) else False

    def _load_model_state_dict(self, state_dict):
        if self._check_is_parallel():
            self._model.module.load_state_dict(state_dict)
        else:
            self._model.load_state_dict(state_dict)

    def _optimizer_to(self, device):
        """
        Move the optimizer tensors to device before training.

        Solves restore issue:
        https://github.com/atomistic-machine-learning/schnetpack/issues/126
        https://github.com/pytorch/pytorch/issues/2830

        """
        for state in self.optimizer.state.values():
            for k, v in state.items():
                if torch.is_tensor(v):
                    state[k] = v.to(device)

    @property
    def state_dict(self):
        state_dict = {
            "epoch": self.epoch,
            "step": self.step,
            "best_loss": self.best_loss,
            "best_losses": self.best_losses,
            "optimizer": self.optimizer.state_dict(),
            "hooks": [h.state_dict for h in self.hooks],
        }
        if self._check_is_parallel():
            state_dict["model"] = self._model.module.state_dict()
        else:
            state_dict["model"] = self._model.state_dict()
        return state_dict

    @state_dict.setter
    def state_dict(self, state_dict):
        self.epoch = state_dict["epoch"]
        self.step = state_dict["step"]
        self.best_loss = state_dict["best_loss"]
        self.best_losses = state_dict["best_losses"]
        self.optimizer.load_state_dict(state_dict["optimizer"])
        self._load_model_state_dict(state_dict["model"])

        for h, s in zip(self.hooks, self.state_dict["hooks"]):
            h.state_dict = s

    def store_checkpoint(self):
        chkpt = os.path.join(
            self.checkpoint_path, "checkpoint-" + str(self.epoch) + ".pth.tar"
        )
        torch.save(self.state_dict, chkpt)

        chpts = [f for f in os.listdir(self.checkpoint_path) if f.endswith(".pth.tar")]
        if len(chpts) > self.keep_n_checkpoints:
            chpt_epochs = [int(f.split(".")[0].split("-")[-1]) for f in chpts]
            sidx = np.argsort(chpt_epochs)
            for i in sidx[: -self.keep_n_checkpoints]:
                os.remove(os.path.join(self.checkpoint_path, chpts[i]))

    def restore_checkpoint(self, epoch=None):
        if epoch is None:
            epoch = max(
                [
                    int(f.split(".")[0].split("-")[-1])
                    for f in os.listdir(self.checkpoint_path)
                    if f.startswith("checkpoint")
                ]
            )

        chkpt = os.path.join(
            self.checkpoint_path, "checkpoint-" + str(epoch) + ".pth.tar"
        )
        self.state_dict = torch.load(chkpt)

    def calc_pred(self, prediction, epoch_losses, batch_num, remember=10):
        preds = []
        preds.append(prediction)
        lene = len(epoch_losses)
        if lene < remember:
            lene2 = -1
        else:
            lene2 = lene - remember
        #for i in range(len(epoch_losses)):
        for i in range(lene - 1, lene2, -1):
            preds.append(epoch_losses[i][batch_num])
        preds = torch.tensor(preds)
        pred = torch.mean(preds)
        print(pred, preds)
        return pred

    def train(self, device, n_epochs=sys.maxsize):
        """Train the model for the given number of epochs on a specified device.

        Args:
            device (torch.torch.Device): device on which training takes place.
            n_epochs (int): number of training epochs.

        Note: Depending on the `hooks`, training can stop earlier than `n_epochs`.

        """
        self._model.to(device)
        self._optimizer_to(device)
        self._stop = False

        for h in self.hooks:
            h.on_train_begin(self)

        try:
            for _ in range(n_epochs):
                # increase number of epochs by 1
                self.epoch += 1
                print('Epoch: ', self.epoch, ' of ', n_epochs)

                for h in self.hooks:
                    h.on_epoch_begin(self)

                if self._stop:
                    # decrease self.epoch if training is aborted on epoch begin
                    self.epoch -= 1
                    break

                # perform training epoch
                #                if progress:
                #                    train_iter = tqdm(self.train_loader)
                #                else:
                self._model.train()

                train_iter = self.train_loader

                self.optimizer.zero_grad()
                step_num = 0
                loss_sum = 0
                batch_num = 0
                for train_batch in train_iter:

                    batch_num += 1
                    step_num += 1
                    print('Batch: ', batch_num, ' of ', len(train_iter))

                    for h in self.hooks:
                        h.on_batch_begin(self, train_batch)

                    # move input to gpu, if needed
                    train_batch = {k: v.to(device) for k, v in train_batch.items()}

                    result = self._model(train_batch)
                    loss = self.loss_fn(train_batch, result) / self.n_acc_steps
                    loss.backward()
                    loss_sum += loss

                    if step_num == self.n_acc_steps or batch_num == len(train_iter):
                        self.optimizer.step()
                        step_num = 0

                        self.step += 1

                        for h in self.hooks:
                            h.on_batch_end(self, train_batch, result, loss_sum)

                        if self._stop:
                            break

                        loss_sum = 0
                        self.optimizer.zero_grad()

                if self.epoch % self.checkpoint_interval == 0:
                    self.store_checkpoint()

                # validation
                self._model.eval()
                if self.epoch % self.validation_interval == 0 or self._stop:
                    for h in self.hooks:
                        h.on_validation_begin(self)

                    val_loss = 0.0
                    n_val = 0

                    if self.ensembleModel:
                        batch_num = 0
                        batch_losses = []

                    for val_batch in self.validation_loader:
                        # append batch_size
                        vsize = list(val_batch.values())[0].size(0)
                        n_val += vsize

                        for h in self.hooks:
                            h.on_validation_batch_begin(self)

                        # move input to gpu, if needed
                        val_batch = {k: v.to(device) for k, v in val_batch.items()}

                        val_result = self._model(val_batch)

                        if self.ensembleModel:
                            prediction = val_result['y']
                            if self.epoch == 1:
                                pred = prediction
                            else:
                                pred = self.calc_pred(prediction, self.epoch_losses, batch_num, self.remember)
                            batch_losses.append(prediction.data)
                            #print(batch_losses.data)
                            batch_num += 1
                            val_result['y'] = pred
                            print('Pred: ', val_result['y'], torch.cuda.memory_allocated(device=None))

                        val_batch_loss = (
                            self.loss_fn(val_batch, val_result).data.cpu().numpy()
                        )
                        #print(batch_losses, 'Pred', pred)
                        if self.loss_is_normalized:
                            val_loss += val_batch_loss * vsize
                        else:
                            val_loss += val_batch_loss

                        for h in self.hooks:
                            h.on_validation_batch_end(self, val_batch, val_result)

                    # weighted average over batches
                    if self.loss_is_normalized:
                        val_loss /= n_val

                    if self.ensembleModel:
                        if len(self.best_losses) < self.remember:
                            self.best_losses.append(val_loss)
                            #self.epoch_losses.append(batch_losses)
                            torch.save(self._model, self.best_model + str(len(self.best_losses) - 1))
                        else:
                            bad_loss = np.argmax(self.best_losses)
                            if self.best_losses[bad_loss] > val_loss:
                                self.best_losses[bad_loss] = val_loss
                                #self.epoch_losses[bad_loss] = batch_losses
                                torch.save(self._model, self.best_model + str(bad_loss))
                    else:
                        if self.best_loss > val_loss:
                            self.best_loss = val_loss
                            torch.save(self._model, self.best_model)

                    for h in self.hooks:
                        h.on_validation_end(self, val_loss)

                    if self.ensembleModel:
                        self.epoch_losses.append(batch_losses)

                for h in self.hooks:
                    h.on_epoch_end(self)

                if self._stop:
                    break
            #
            # Training Ends
            #
            # run hooks & store checkpoint
            for h in self.hooks:
                h.on_train_ends(self)
            self.store_checkpoint()

        except Exception as e:
            for h in self.hooks:
                h.on_train_failed(self)

            raise e
