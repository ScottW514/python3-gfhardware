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
#include <stdio.h>
#include <jpeglib.h>

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
struct cam_control cam_controls[] = {
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

static PyObject *method_capture(PyObject *self, PyObject *args, PyObject* kwds) {
  struct buffer *buffers;

  int cam_sel = 0;
  int exposure = 3000;
  int gain = 30;

  /* Parse arguments */
  static char* argnames[] = {"cam_sel", "exposure", "gain", NULL};
  if(!PyArg_ParseTupleAndKeywords(args, kwds, "|iii",
           argnames, &cam_sel, &exposure, &gain)) {
      return NULL;
  }
  if (cam_sel < 0 | cam_sel > 1)
    return PyErr_Format(PyExc_ValueError, "cam_sel must be between 0 and 1");
  if (exposure < 0 | exposure > 65535)
    return PyErr_Format(PyExc_ValueError, "exposure must be between 0 and 65535");
  if (gain < 0 | gain > 1023)
    return PyErr_Format(PyExc_ValueError, "gain must be between 0 and 1023");

  // Open device
  int dev_fd = open(GFCAM_DEV_PATH, O_RDWR | O_NONBLOCK, 0);
  if (dev_fd == -1)
    return PyErr_Format(PyExc_IOError, "failed to open %s", GFCAM_DEV_PATH);

  // Verify that we are set for CSI->MEM
  int index;
  if (_ioctl(dev_fd, VIDIOC_G_INPUT, &index) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_G_INPUT failed (%x)", VIDIOC_G_INPUT);

  struct v4l2_input input;
  memset(&input, 0, sizeof(input));
  input.index = index;
  if (_ioctl(dev_fd, VIDIOC_ENUMINPUT, &input) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_ENUMINPUT failed (%x)", VIDIOC_ENUMINPUT);
  if (strcmp((const char *)input.name, "CSI MEM") != 0)
    return PyErr_Format(PyExc_IOError, "video input (%s) is not 'CSI MEM' ", input.name);

  // Select camera
  struct v4l2_control ctrl;
  CLEAR(ctrl);
  ctrl.id = V4L2_CID_GLOWFORGE_SEL_CAM;
  ctrl.value = cam_sel;
  if (_ioctl(dev_fd, VIDIOC_S_CTRL, &ctrl) < 0)
    return PyErr_Format(PyExc_IOError, "failed to select camera %d", cam_sel);

  // Validate Camera capabilites
  struct v4l2_capability cap;
  if (_ioctl(dev_fd, VIDIOC_QUERYCAP, &cap) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_QUERYCAP failed");
  if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE))
    return PyErr_Format(PyExc_IOError, "%s is not capture device", GFCAM_DEV_PATH);
  if (!(cap.capabilities & V4L2_CAP_STREAMING))
    return PyErr_Format(PyExc_IOError, "%s is not a streaming device", GFCAM_DEV_PATH);

  // Set capture parameters
  struct v4l2_streamparm strmparm;
  CLEAR(strmparm);
  strmparm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  strmparm.parm.capture.timeperframe.numerator = 1;
  strmparm.parm.capture.timeperframe.denominator = 15;
  strmparm.parm.capture.capturemode = 4;
  if (_ioctl(dev_fd, VIDIOC_S_PARM, &strmparm) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_PARM failed");

  // Set camera controls
  for(int i = 0; i < NUM_CAM_CONTROLS; i++) {
    struct v4l2_control ctrl;
    CLEAR(ctrl);
    ctrl.id = cam_controls[i].cid;

    if (strcmp(cam_controls[i].name, "exposure") == 0)
      ctrl.value = exposure;
    else if (strcmp(cam_controls[i].name, "gain") == 0)
      ctrl.value = gain;
    else
      ctrl.value = cam_controls[i].value;

    if (_ioctl(dev_fd, VIDIOC_S_CTRL, &ctrl) < 0) {
      PyErr_Format(PyExc_IOError,
        "VIDIOC_S_CTRL failed (%x/%d)",
        cam_controls[i].cid, cam_controls[i].value);
      return NULL;
    }
  }

  // Set capture cropping
  struct v4l2_cropcap cropcap;
  CLEAR(cropcap);
  cropcap.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(dev_fd, VIDIOC_CROPCAP, &cropcap) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_CROPCAP failed");

  struct v4l2_crop crop;
  CLEAR(crop);
  crop.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  crop.c.top = 0;
  crop.c.left = 0;
  crop.c.width = GFCAM_WIDTH;
  crop.c.height = GFCAM_HEIGHT;
  if (_ioctl(dev_fd, VIDIOC_S_CROP, &crop) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_CROP failed");

  // Set capture format
  struct v4l2_format fmt;
  CLEAR(fmt);
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  fmt.fmt.pix.width = GFCAM_WIDTH;
  fmt.fmt.pix.height = GFCAM_HEIGHT;
  fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_SBGGR8;
  if (_ioctl(dev_fd, VIDIOC_S_FMT, &fmt) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_S_FMT failed");

  // Initialize buffers
  struct v4l2_requestbuffers req;
  CLEAR(req);
  req.count = 2;
  req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  req.memory = V4L2_MEMORY_MMAP;

  if (_ioctl(dev_fd, VIDIOC_REQBUFS, &req) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_REQBUFS failed");
  if (req.count < 2)
    return PyErr_Format(PyExc_IOError, "Insufficient buffers");

  buffers = calloc(req.count, sizeof(*buffers));
  if (!buffers)
    return PyErr_Format(PyExc_MemoryError, "failed to allocate buffers");

  struct v4l2_buffer buf;
  int n_buffers;
  for (n_buffers = 0; n_buffers < (int)req.count; ++n_buffers) {
    CLEAR(buf);
    buf.type        = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory      = V4L2_MEMORY_MMAP;
    buf.index       = n_buffers;
    if (_ioctl(dev_fd, VIDIOC_QUERYBUF, &buf) < 0)
      return PyErr_Format(PyExc_IOError, "VIDIOC_QUERYBUF failed");

    buffers[n_buffers].length = buf.length;
    buffers[n_buffers].start = mmap(NULL, buf.length,
              PROT_READ | PROT_WRITE, MAP_SHARED, dev_fd, buf.m.offset);

    if (MAP_FAILED == buffers[n_buffers].start)
      return PyErr_Format(PyExc_IOError, "mmap failed");
  }

  // Queue buffers
  for (int i = 0; i < n_buffers; ++i) {
    CLEAR(buf);
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    buf.index = i;

    if (_ioctl(dev_fd, VIDIOC_QBUF, &buf) < 0)
      return PyErr_Format(PyExc_IOError, "VIDIOC_QBUF failed");
  }

  // Stream On
  enum v4l2_buf_type type;
  type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(dev_fd, VIDIOC_STREAMON, &type) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_QBUF failed");

  // Select Loop
  for (;;) {
    fd_set fds;
    struct timeval tv;
    int ret;

    FD_ZERO(&fds);
    FD_SET(dev_fd, &fds);

    tv.tv_sec = 2;
    tv.tv_usec = 0;

    ret = select(dev_fd + 1, &fds, NULL, NULL, &tv);

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

    if (_ioctl(dev_fd, VIDIOC_DQBUF, &buf) < 0) {
      if ((errno == EAGAIN) | (errno == EIO))
        continue;
      else
        return PyErr_Format(PyExc_IOError, "VIDIOC_DQBUF failed");
    } else
      break;
  }

  // Convert Bayer to RGB
  uint32_t rgb_size = GFCAM_WIDTH * GFCAM_HEIGHT * 3;
  void *rgb_map = mmap(NULL, rgb_size, PROT_READ | PROT_WRITE,
        MAP_SHARED | MAP_ANONYMOUS, -1, 0);
  if(rgb_map == MAP_FAILED) {
    PyErr_Format(PyExc_MemoryError, "rgb_map mmap failed");
    return NULL;
  }
  dc1394_bayer_decoding_8bit(
    (const uint8_t*)buffers[buf.index].start,
    (uint8_t*)rgb_map, GFCAM_WIDTH, GFCAM_HEIGHT,
    DC1394_COLOR_FILTER_BGGR, DC1394_BAYER_METHOD_BILINEAR);

  // Stop stream
  type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (_ioctl(dev_fd, VIDIOC_STREAMOFF, &type) < 0)
    return PyErr_Format(PyExc_IOError, "VIDIOC_STREAMOFF failed");

  // Release Capture Buffers
  Py_BEGIN_ALLOW_THREADS
  for(int i = 0; i < n_buffers; i++) {
    munmap(buffers[i].start, buffers[i].length);
  }
  free(buffers);
  close(dev_fd);
  Py_END_ALLOW_THREADS

  unsigned char *jpg_buf = NULL;
  unsigned long jpg_buf_size = 0;
  struct jpeg_compress_struct jpg_cinfo;
	struct jpeg_error_mgr jpg_err;
  JSAMPROW row_pointer[1];
  jpg_cinfo.err = jpeg_std_error(&jpg_err);
	jpeg_create_compress(&jpg_cinfo);
  jpeg_mem_dest(&jpg_cinfo, &jpg_buf, &jpg_buf_size);

  jpg_cinfo.image_width = GFCAM_WIDTH;
	jpg_cinfo.image_height = GFCAM_HEIGHT;
	jpg_cinfo.input_components = 3;
	jpg_cinfo.in_color_space = JCS_RGB;
	jpeg_set_defaults(&jpg_cinfo);
	jpeg_set_quality(&jpg_cinfo, 75, TRUE);
	jpeg_start_compress(&jpg_cinfo, TRUE);

  Py_BEGIN_ALLOW_THREADS
	while (jpg_cinfo.next_scanline < jpg_cinfo.image_height) {
    row_pointer[0] = &rgb_map[jpg_cinfo.next_scanline * GFCAM_WIDTH * 3];
    jpeg_write_scanlines(&jpg_cinfo, row_pointer, 1);
	}
	jpeg_finish_compress(&jpg_cinfo);
	jpeg_destroy_compress(&jpg_cinfo);
  Py_END_ALLOW_THREADS

  PyObject *result = PyBytes_FromStringAndSize(jpg_buf, jpg_buf_size);

  Py_BEGIN_ALLOW_THREADS
  free(jpg_buf);
  munmap(rgb_map, rgb_size);
  Py_END_ALLOW_THREADS

  return result;
}

static PyMethodDef module_methods[] = {
  {"capture", (PyCFunctionWithKeywords)method_capture,
    METH_VARARGS | METH_KEYWORDS,
    "capture(cam_sel: int = 0, exposure: int = 3000, gain: int = 30) -> bytes\n\n"
    "Reads image from selected camera, returns as JPEG"},
  {NULL}
};

PyMODINIT_FUNC PyInit_cam(void) {
  static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "cam", "Glowforge Camera interface module",
    -1, module_methods, NULL, NULL, NULL, NULL
  };
  PyObject *module = PyModule_Create(&moduledef);

  if(!module)
    return NULL;

  PyModule_AddIntMacro(module, GFCAM_LID);
  PyModule_AddIntMacro(module, GFCAM_HEAD);
  PyModule_AddIntMacro(module, GFCAM_WIDTH);
  PyModule_AddIntMacro(module, GFCAM_HEIGHT);

  return module;
}
