/*
 * renfield_isp_capture — grab ONE frame from the Allwinner A733 sunxi-vin + ISP
 * pipeline and write it as raw I420 (YUV420 planar) to a file.
 *
 * WHY: on the A733 (Orange Pi Zero 3W) the CSI camera only produces frames when the
 * Allwinner ISP userspace is running (libAWIspApi/libisp). Plain V4L2/OpenCV can't
 * capture (the ISP/scaler pipeline won't negotiate). This does the exact sequence the
 * vendor AWISPdemo does, but non-interactively and writing a clean frame — so the
 * satellite can grab occupancy/gesture frames. See docs/design/a733-satellite-camera.md.
 *
 * Build (on the board): gcc -O2 -o renfield_isp_capture renfield_isp_capture.c \
 *                          -L/opt/awisp/lib -lAWIspApi -Wl,-rpath,/opt/awisp/lib
 * Run:   renfield_isp_capture /dev/video0 640 480 /tmp/frame.i420 [warmup_frames=8]
 * Output: raw I420 (Y w*h, then U w*h/4, then V w*h/4). Convert in Python.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <linux/videodev2.h>
#include "AWIspApi.h"

/* sunxi private ioctl (from sunxi_camera_v2.h): BASE_VIDIOC_PRIVATE(192) + 15 */
struct sensor_isp_cfg { unsigned char isp_wdr_mode; unsigned char large_image; };
#define VIDIOC_SET_SENSOR_ISP_CFG _IOWR('V', BASE_VIDIOC_PRIVATE + 15, struct sensor_isp_cfg)

#define NBUF 4
#define MAXPLANES 3

static int xioctl(int fd, unsigned long req, void *arg, const char *name, int fatal) {
    int r = ioctl(fd, req, arg);
    if (r < 0) {
        fprintf(stderr, "[cap] %s failed: %s\n", name, strerror(errno));
        if (fatal) exit(2);
    }
    return r;
}

int main(int argc, char **argv) {
    const char *dev = argc > 1 ? argv[1] : "/dev/video0";
    int W = argc > 2 ? atoi(argv[2]) : 640;
    int H = argc > 3 ? atoi(argv[3]) : 480;
    const char *out = argc > 4 ? argv[4] : "/tmp/frame.i420";
    int warmup = argc > 5 ? atoi(argv[5]) : 8;   /* skip frames while 3A converges */

    int fd = open(dev, O_RDWR | O_NONBLOCK, 0);
    if (fd < 0) { fprintf(stderr, "[cap] open %s: %s\n", dev, strerror(errno)); return 2; }

    int input = 0;
    xioctl(fd, VIDIOC_S_INPUT, &input, "S_INPUT", 0);

    struct sensor_isp_cfg icfg = {0, 0};
    xioctl(fd, VIDIOC_SET_SENSOR_ISP_CFG, &icfg, "SET_SENSOR_ISP_CFG", 0);

    struct v4l2_streamparm parm; memset(&parm, 0, sizeof parm);
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = 30;
    xioctl(fd, VIDIOC_S_PARM, &parm, "S_PARM", 0);

    struct v4l2_format fmt; memset(&fmt, 0, sizeof fmt);
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    fmt.fmt.pix_mp.width = W;
    fmt.fmt.pix_mp.height = H;
    fmt.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_YUV420M;  /* I420, 3 separate planes */
    fmt.fmt.pix_mp.field = V4L2_FIELD_NONE;
    fmt.fmt.pix_mp.num_planes = 3;
    xioctl(fd, VIDIOC_S_FMT, &fmt, "S_FMT", 1);
    xioctl(fd, VIDIOC_G_FMT, &fmt, "G_FMT", 0);
    W = fmt.fmt.pix_mp.width; H = fmt.fmt.pix_mp.height;
    int nplanes = fmt.fmt.pix_mp.num_planes;
    fprintf(stderr, "[cap] fmt %dx%d planes=%d\n", W, H, nplanes);

    struct v4l2_requestbuffers req; memset(&req, 0, sizeof req);
    req.count = NBUF; req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; req.memory = V4L2_MEMORY_MMAP;
    xioctl(fd, VIDIOC_REQBUFS, &req, "REQBUFS", 1);

    void  *pbuf[NBUF][MAXPLANES];
    size_t plen[NBUF][MAXPLANES];
    for (unsigned i = 0; i < req.count; i++) {
        struct v4l2_buffer buf; struct v4l2_plane planes[MAXPLANES];
        memset(&buf, 0, sizeof buf); memset(planes, 0, sizeof planes);
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; buf.memory = V4L2_MEMORY_MMAP;
        buf.index = i; buf.length = nplanes; buf.m.planes = planes;
        xioctl(fd, VIDIOC_QUERYBUF, &buf, "QUERYBUF", 1);
        for (int p = 0; p < nplanes; p++) {
            plen[i][p] = planes[p].length;
            pbuf[i][p] = mmap(NULL, planes[p].length, PROT_READ | PROT_WRITE, MAP_SHARED,
                              fd, planes[p].m.mem_offset);
            if (pbuf[i][p] == MAP_FAILED) { perror("[cap] mmap"); return 2; }
        }
        xioctl(fd, VIDIOC_QBUF, &buf, "QBUF", 1);
    }

    /* Start the Allwinner ISP userspace — without this the pipeline produces nothing. */
    AWIspApi *isp = CreateAWIspApi();
    if (!isp) { fprintf(stderr, "[cap] CreateAWIspApi failed\n"); return 2; }
    isp->ispApiInit();
    int isp_id = isp->ispGetIspId(0);
    isp->ispStart(isp_id);
    fprintf(stderr, "[cap] isp started id=%d\n", isp_id);

    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    xioctl(fd, VIDIOC_STREAMON, &type, "STREAMON", 1);

    int got = 0;
    for (int n = 0; n <= warmup; n++) {
        fd_set fds; FD_ZERO(&fds); FD_SET(fd, &fds);
        struct timeval tv = {2, 0};
        if (select(fd + 1, &fds, NULL, NULL, &tv) <= 0) { fprintf(stderr, "[cap] select timeout\n"); break; }

        struct v4l2_buffer buf; struct v4l2_plane planes[MAXPLANES];
        memset(&buf, 0, sizeof buf); memset(planes, 0, sizeof planes);
        buf.type = type; buf.memory = V4L2_MEMORY_MMAP; buf.length = nplanes; buf.m.planes = planes;
        if (xioctl(fd, VIDIOC_DQBUF, &buf, "DQBUF", 0) < 0) { usleep(20000); continue; }

        if (n == warmup) {                       /* keep the last (3A-settled) frame */
            FILE *f = fopen(out, "wb");
            if (!f) { fprintf(stderr, "[cap] fopen %s: %s\n", out, strerror(errno)); return 2; }
            size_t ysz = (size_t)W * H, csz = ysz / 4;
            fwrite(pbuf[buf.index][0], 1, ysz, f);
            fwrite(pbuf[buf.index][1], 1, csz, f);
            fwrite(pbuf[buf.index][2], 1, csz, f);
            fclose(f);
            got = 1;
            fprintf(stderr, "[cap] wrote %s (%dx%d I420)\n", out, W, H);
        }
        xioctl(fd, VIDIOC_QBUF, &buf, "QBUF", 0);
    }

    xioctl(fd, VIDIOC_STREAMOFF, &type, "STREAMOFF", 0);
    isp->ispStop(isp_id);
    isp->ispApiUnInit();
    DestroyAWIspApi(isp);
    close(fd);
    return got ? 0 : 1;
}
