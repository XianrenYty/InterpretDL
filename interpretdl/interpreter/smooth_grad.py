import typing
from typing import Any, Callable, List, Tuple, Union

import IPython.display as display
import cv2
import numpy as np
import os, sys
from PIL import Image

from .abc_interpreter import Interpreter
from ..data_processor.readers import preprocess_image, read_image
from ..data_processor.visualizer import visualize_ig


class SmoothGradInterpreter(Interpreter):
    """
    Smooth Gradients Interpreter.

    More details regarding the Smooth Gradients method can be found in the original paper:
    http://arxiv.org/pdf/1706.03825.pdf
    """

    def __init__(self,
                 paddle_model,
                 trained_model_path,
                 use_cuda=True,
                 model_input_shape=[3, 224, 224]):
        """
        Initialize the SmoothGradInterpreter.

        Args:
            paddle_model (callable): A user-defined function that gives access to model predictions.
                    It takes the following arguments:

                    - data: Data input.
                    and outputs predictions. See the example at the end of ``interpret()``.
            trained_model_path (str): The pretrained model directory.
            use_cuda (bool, optional): Whether or not to use cuda. Default: True
            model_input_shape (list, optional): The input shape of the model. Default: [3, 224, 224]
        """
        Interpreter.__init__(self)
        self.paddle_model = paddle_model
        self.trained_model_path = trained_model_path
        self.use_cuda = use_cuda
        self.model_input_shape = model_input_shape
        self.data_type = 'float32'
        self.paddle_prepared = False

    def interpret(self,
                  data,
                  label=None,
                  noise_amout=0.1,
                  n_samples=50,
                  visual=True,
                  save_path=None):
        """
        Main function of the interpreter.

        Args:
            data (str or numpy.ndarray): The image filepath or a numpy array.
            label (int, optional): The target label to analyze. If None, the most likely label will be used. Default: None
            noise_amount (float, optional): Noise level of added noise to the image.
                                            The std of Guassian random noise is noise_amount * (x_max - x_min). Default: 0.1
            n_samples (int, optional): The number of new images generated by adding noise. Default: 50
            visual (bool, optional): Whether or not to visualize the processed image. Default: True
            save_path (str, optional): The filepath to save the processed image. If None, the image will not be saved. Default: None

        Returns:
            numpy.ndarray: avg_gradients

        Example::

            def paddle_model(data):
                import paddle.fluid as fluid
                class_num = 1000
                model = ResNet50()
                logits = model.net(input=image_input, class_dim=class_num)
                probs = fluid.layers.softmax(logits, axis=-1)
                return probs
            sg = SmoothGradInterpreter(paddle_model, "assets/ResNet50_pretrained")
            gradients = sg.interpret(img_path, visual=True, save_path='sg_test.jpg')
        """

        # Read in image
        if isinstance(data, str):
            img = read_image(data, crop_size=self.model_input_shape[1])
            data = preprocess_image(img)

        data_type = np.array(data).dtype
        self.data_type = data_type

        if not self.paddle_prepared:
            self._paddle_prepare()

        if label is None:
            _, out = self.predict_fn(data, 0)
            label = np.argmax(out[0])

        std = noise_amout * (np.max(data) - np.min(data))
        total_gradients = np.zeros_like(data)
        for i in range(n_samples):
            noise = np.float32(np.random.normal(0.0, std, data.shape))
            gradients, out = self.predict_fn(data, label, noise)
            total_gradients += np.array(gradients)

        avg_gradients = total_gradients / n_samples

        if visual:
            visualize_ig(avg_gradients, img, visual, save_path)

        return avg_gradients

    def _paddle_prepare(self, predict_fn=None):
        if predict_fn is None:
            import paddle.fluid as fluid
            startup_prog = fluid.Program()
            main_program = fluid.Program()
            with fluid.program_guard(main_program, startup_prog):
                with fluid.unique_name.guard():
                    data_op = fluid.data(
                        name='data',
                        shape=[1] + self.model_input_shape,
                        dtype=self.data_type)
                    label_op = fluid.data(
                        name='label', shape=[1, 1], dtype='int64')
                    x_noise = fluid.data(
                        name='noise',
                        shape=[1] + self.model_input_shape,
                        dtype='float32')

                    x_plus_noise = data_op + x_noise
                    probs = self.paddle_model(x_plus_noise)

                    for op in main_program.global_block().ops:
                        if op.type == 'batch_norm':
                            op._set_attr('use_global_stats', True)
                        elif op.type == 'dropout':
                            op._set_attr('dropout_prob', 0.0)

                    class_num = probs.shape[-1]
                    one_hot = fluid.layers.one_hot(label_op, class_num)
                    one_hot = fluid.layers.elementwise_mul(probs, one_hot)
                    target_category_loss = fluid.layers.reduce_sum(one_hot)

                    p_g_list = fluid.backward.append_backward(
                        target_category_loss)
                    gradients_map = fluid.gradients(one_hot, x_plus_noise)[0]

            if self.use_cuda:
                gpu_id = int(os.environ.get('FLAGS_selected_gpus', 0))
                place = fluid.CUDAPlace(gpu_id)
            else:
                place = fluid.CPUPlace()
            exe = fluid.Executor(place)
            fluid.io.load_persistables(exe, self.trained_model_path,
                                       main_program)

            def predict_fn(data, label_index, noise=0.0):
                if isinstance(noise, (float, int)):
                    noise = np.ones_like(data) * noise
                gradients, out = exe.run(main_program,
                                         feed={
                                             'data': data,
                                             'label':
                                             np.array([[label_index]]),
                                             'noise': noise
                                         },
                                         fetch_list=[gradients_map, probs])
                return gradients, out

        self.predict_fn = predict_fn
        self.paddle_prepared = True
