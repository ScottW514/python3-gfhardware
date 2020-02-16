/*
 * python-gfhardware cam module
 * Python extension to capture images from Glowforge hardware
 * Copyright 2020, Scott Wiederhold <s.e.wiederhold@gmail.com>
 * Released under the MIT license.
 * SPDX-License-Identifier: MIT
 *
 * Portions Based on python-v4l2capture
 * 2009, 2010, 2011 Fredrik Portstrom
 * and V4L2 sample kernel code at "Documentation/media/uapi/v4l/v4l2grab.c",
 * both in the public domain.
 *
 */
#include <Python.h>
#include <fcntl.h>
#include <string.h>
#include <linux/videodev2.h>
#include <sys/mman.h>
#include <libv4l2.h>

#include "bayer.h"

#define V4L2_CID_GLOWFORGE_SEL_CAM (V4L2_CID_PRIVATE_BASE + 8)

#define GFCAM_DEV_PATH "/dev/video0"
#define GFCAM_WIDTH 2592
#define GFCAM_HEIGHT 1944
#define GFCAM_LID  0
#define GFCAM_HEAD 1

#define CLEAR(x) memset(&(x), 0, sizeof(x))

struct buffer {
  void *start;
  size_t length;
};

typedef struct {
  PyObject_HEAD
  int fd;
  struct buffer *buffers;
  int n_buffers;
  int setup;
  int cam_sel;
} cam_device;

struct cam_control {
  const int cid;
  const char *name;
  const int value;
};


#define NUM_CAM_CONTROLS 11
const struct cam_control cam_controls[] = {
  {V4L2_CID_EXPOSURE_AUTO, "exposure-auto", 0},
  {V4L2_CID_EXPOSURE, "exposure", 3000},
  {V4L2_CID_AUTOGAIN, "gain-auto", 0},
  {V4L2_CID_GAIN, "gain", 30},
  {V4L2_CID_AUTO_WHITE_BALANCE, "white-balance-auto", 2},
  {V4L2_CID_RED_BALANCE, "red-balance", 1100},
  {V4L2_CID_BLUE_BALANCE, "blue-balance", 1400},
  {V4L2_CID_FLASH_LED_MODE, "flash-led-mode", 2},
  {V4L2_CID_FLASH_TORCH_INTENSITY, "flash-intensity", 0},
  {V4L2_CID_HFLIP, "flip-h", 1},
  {V4L2_CID_VFLIP, "flip-v", 0},
};

static int _ioctl(int fd, int request, void *arg) {
  for(;;) {
    int result = v4l2_ioctl(fd, request, arg);
    if(!result)
      return 0;
    if(errno != EINTR) {
      return errno;
    }
  }
}

static int _set_controls(cam_device *self) {
  struct v4l2_control ctrl;
  for(int i = 0; i < NUM_CAM_CONTROLS; i++) {
    CLEAR(ctrl);
    ctrl.id = cam_controls[i].cid;
    ctrl.value = cam_controls[i].value;
    if (_ioctl(self->fd, VIDIOC_S_CTRL, &ctrl) < 0) {
      PyErr_Format(PyExc_IOError,
        "VIDIOC_S_CTRL failed (%x/%d)",
        cam_controls[i].cid, cam_controls[i].value);
      return 0;
    }
  }
  return 1;
}

static PyObject *capture(cam_device *self) {
  struct v4l2_input input;
  struct v4l2_capability cap;
  struct v4l2_cropcap cropcap;
  struct v4l2_crop crop;
  struct v4l2_format fmt;
  struct v4l2_streamparm strmparm;
  struct v4l2_control ctrl;
  struct v4l2_requestbuffers req;
  enum v4l2_buf_type type;
  struct v4l2_buffer buf;
  void *rgb_map = NULL;
  uint32_t rgb_size = 0;
  int index;

  // Open device
  self->fd = open(GFCAM_DEV_PATH, O_RDWR | O_NONBLOCK, 0);
  if (self->fd == -1)
    return PyErr_Format(PyExc_IOError, "failed to open %s", GFCAM_DEV_PATH);

  // Verify that we are set for CSI->MEM
  if (_ioctl(self->fd, VIDIOC_G_INPUT, &index) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_G_INPUT failed (%x)", VIDIOC_G_INPUT);
  memset(&input, 0, sizeof(input));
  input.index = index;
  if (_ioctl(self->fd, VIDIOC_ENUMINPUT, &input) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_ENUMINPUT failed (%x)", VIDIOC_ENUMINPUT);
  if (strcmp((const char *)input.name, "CSI MEM") != 0)
    return PyErr_Format(PyExc_IOError, "video input (%s) is not 'CSI MEM' ", input.name);

  // Select camera
  CLEAR(ctrl);
  ctrl.id = V4L2_CID_GLOWFORGE_SEL_CAM;
  ctrl.value = self->cam_sel;
  if (_ioctl(self->fd, VIDIOC_S_CTRL, &ctrl) < 0)
    return PyErr_Format(PyExc_IOError, "failed to select camera %d", self->cam_sel);

  // Validate Camera capabilites
  if (_ioctl(self->fd, VIDIOC_QUERYCAP, &cap) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_QUERYCAP failed");
  if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE))
    return PyErr_Format(PyExc_IOError, "%s is not capture device", GFCAM_DEV_PATH);
  if (!(cap.capabilities & V4L2_CAP_STREAMING))
    return PyErr_Format(PyExc_IOError, "%s is not a streaming device", GFCAM_DEV_PATH);

  // Set capture parameters
  CLEAR(strmparm);
  strmparm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  strmparm.parm.capture.timeperframe.numerator = 1;
  strmparm.parm.capture.timeperframe.denominator = 15;
  strmparm.parm.capture.capturemode = 4;
  if (_ioctl(self->fd, VIDIOC_S_PARM, &strmparm) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_PARM failed");

  // Set camera controls
  if (!_set_controls(self))
    return NULL;

  // Set capture cropping
  CLEAR(cropcap);
  cropcap.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(self->fd, VIDIOC_CROPCAP, &cropcap) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_CROPCAP failed");
  CLEAR(crop);
  crop.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  crop.c.top = 0;
  crop.c.left = 0;
  crop.c.width = GFCAM_WIDTH;
  crop.c.height = GFCAM_HEIGHT;
  if (_ioctl(self->fd, VIDIOC_S_CROP, &crop) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_CROP failed");

  // Set capture format
  CLEAR(fmt);
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  fmt.fmt.pix.width = GFCAM_WIDTH;
  fmt.fmt.pix.height = GFCAM_HEIGHT;
  fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_SBGGR8;
  if (_ioctl(self->fd, VIDIOC_S_FMT, &fmt) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_FMT failed");

  // Initialize buffers
  CLEAR(req);
  req.count = 2;
  req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  req.memory = V4L2_MEMORY_MMAP;

  if (_ioctl(self->fd, VIDIOC_REQBUFS, &req) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_REQBUFS failed");
  if (req.count < 2)
    return PyErr_Format(PyExc_IOError, "Insufficient buffers");

  self->buffers = calloc(req.count, sizeof(*self->buffers));
  if (!self->buffers)
    return PyErr_Format(PyExc_MemoryError, "failed to allocate buffers");

  for (self->n_buffers = 0; self->n_buffers < (int)req.count; ++self->n_buffers) {
    CLEAR(buf);
    buf.type        = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory      = V4L2_MEMORY_MMAP;
    buf.index       = self->n_buffers;
    if (_ioctl(self->fd, VIDIOC_QUERYBUF, &buf) < 0)
      return PyErr_Format(PyExc_IOError, "VIDIOC_QUERYBUF failed");

    self->buffers[self->n_buffers].length = buf.length;
    self->buffers[self->n_buffers].start = mmap(NULL, buf.length,
              PROT_READ | PROT_WRITE, MAP_SHARED, self->fd, buf.m.offset);

    if (MAP_FAILED == self->buffers[self->n_buffers].start)
      return PyErr_Format(PyExc_IOError, "mmap failed");
  }

  // Queue buffers
  for (int i = 0; i < self->n_buffers; ++i) {
    CLEAR(buf);
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    buf.index = i;

    if (_ioctl(self->fd, VIDIOC_QBUF, &buf) < 0)
      return PyErr_Format(PyExc_IOError, "VIDIOC_QBUF failed");
  }

  // Stream On
  type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(self->fd, VIDIOC_STREAMON, &type) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_QBUF failed");

  // Select Loop
  for (;;) {
    fd_set fds;
    struct timeval tv;
    int ret;

    FD_ZERO(&fds);
    FD_SET(self->fd, &fds);

    tv.tv_sec = 2;
    tv.tv_usec = 0;

    ret = select(self->fd + 1, &fds, NULL, NULL, &tv);

    if (ret == -1) {
      if (EINTR == errno)
        continue;
      return PyErr_Format(PyExc_IOError, "select failed");
    }

    if (ret == 0)
      return PyErr_Format(PyExc_IOError, "select timeout");

    CLEAR(buf);
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;

    if (_ioctl(self->fd, VIDIOC_DQBUF, &buf) < 0) {
      if ((errno == EAGAIN) | (errno == EIO))
        continue;
      else
        return PyErr_Format(PyExc_IOError, "VIDIOC_DQBUF failed");
    } else
      break;
  }

  // Process Image
  rgb_size = GFCAM_WIDTH * GFCAM_HEIGHT * 3;

  rgb_map = mmap(NULL, rgb_size, PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if(rgb_map == MAP_FAILED) {
    PyErr_Format(PyExc_MemoryError, "rgb_map mmap failed");
    return NULL;
  }
  // Convert Bayer to RGB
  dc1394_bayer_decoding_8bit(
    (const uint8_t*)self->buffers[buf.index].start,
    (uint8_t*)rgb_map, GFCAM_WIDTH, GFCAM_HEIGHT,
    DC1394_COLOR_FILTER_BGGR, DC1394_BAYER_METHOD_BILINEAR);

  PyObject *result = PyBytes_FromStringAndSize(rgb_map, rgb_size);

  if(!result)
    return NULL;

  type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(self->fd, VIDIOC_STREAMOFF, &type) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_STREAMOFF failed");
  munmap(rgb_map, rgb_size);

  return result;
}

static void _uninit_camera(cam_device *self) {
  if (self->fd > 0)
    close(self->fd);
  if (self->buffers) {
    for(int i = 0; i < self->n_buffers; i++) {
      munmap(self->buffers[i].start, self->buffers[i].length);
    }
    free(self->buffers);
  }
  Py_TYPE(self)->tp_free((PyObject *)self);
}

static int _init_camera(cam_device *self, PyObject *args, PyObject *kwargs) {
  self->buffers = NULL;
  self->setup = 0;

  int cam_sel;
  if(!PyArg_ParseTuple(args, "i", &cam_sel))
    self->cam_sel = 0;
  if (cam_sel) {
    self->cam_sel = 1;
  }
  return 0;
}

static PyMethodDef camera_methods[] = {
  {"capture", (PyCFunction)capture, METH_NOARGS,
       "capture() -> string\n\n"
       "Reads image data from camera"},
  {NULL}
};

static PyTypeObject camera_type = {
  PyVarObject_HEAD_INIT(NULL, 0)
  "cam.GFCam", sizeof(cam_device), 0,
  (destructor)_uninit_camera, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
  0, Py_TPFLAGS_DEFAULT, "GFCam(camera)\n\n"
  "Initializes selected camera and returns object" , 0, 0, 0, 0, 0, 0,
  camera_methods, 0, 0, 0, 0, 0, 0, 0,
  (initproc)_init_camera
};

static PyMethodDef module_methods[] = {
  {NULL}
};

PyMODINIT_FUNC PyInit_cam(void) {
  camera_type.tp_new = PyType_GenericNew;

  if(PyType_Ready(&camera_type) < 0)
    return NULL;

  static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "cam", "Glowforge Camera interface module",
    -1, module_methods, NULL, NULL, NULL, NULL
  };
  PyObject *module = PyModule_Create(&moduledef);

  if(!module)
    return NULL;

  Py_INCREF(&camera_type);
  PyModule_AddObject(module, "GFCam", (PyObject *)&camera_type);

  PyModule_AddIntMacro(module, GFCAM_LID);
  PyModule_AddIntMacro(module, GFCAM_HEAD);
  PyModule_AddIntMacro(module, GFCAM_WIDTH);
  PyModule_AddIntMacro(module, GFCAM_HEIGHT);

  return module;
}
