/* tabs9-capture: PipeWire screencast consumer that returns KWin's buffer
 * inside the process callback.
 *
 * Why this exists (measured, see docs/performance.md): pipewire 1.6 recycles
 * at most one input buffer per graph cycle, and only after the consumer's
 * process callback has returned.  GStreamer's pipewiresrc hands buffers to a
 * streaming thread and releases them later, so any hiccup moves one of KWin's
 * 2..4 buffers to the consumer for good; once KWin holds none it records a
 * burst of N frames, then waits ~80 ms for the batch to trickle back, at half
 * the refresh rate forever.  Here the DMA-BUF is imported once per buffer,
 * converted on the GPU into an owned NV12 surface, and queued back to
 * PipeWire before the callback returns, so the same cycle recycles it and
 * KWin never runs dry.  The converted surface is handed to the host (which
 * encodes it with GStreamer) only after the conversion has completed.
 *
 * Protocol (unix socket --sock-fd; PipeWire remote --pw-fd, both inherited):
 *   helper -> host  "ring" message: struct ring_msg followed by SCM_RIGHTS
 *                   with one DMA-BUF fd per slot (ring of NV12 surfaces).
 *   helper -> host  struct frame_msg per converted frame.
 *   host -> helper  struct free_msg when the encoder is done with a slot.
 * Nothing here ever blocks in the PipeWire process callback: no GPU waits,
 * no socket writes.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/raw.h>
#include <spa/debug/types.h>
#include <spa/pod/builder.h>
#include <spa/utils/result.h>

#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_vpp.h>
#include <drm_fourcc.h>

#define MAX_SLOTS 8
#define MAX_PW_BUFFERS 8
#define MSG_MAGIC_RING 0x52494e47u  /* RING */
#define MSG_MAGIC_FRAME 0x4652414du /* FRAM */
#define MSG_MAGIC_FREE 0x46524545u  /* FREE */

struct ring_msg {
	uint32_t magic;
	uint32_t slots;
	uint32_t width, height;
	uint64_t modifier;
	uint32_t offsets[MAX_SLOTS][2];
	uint32_t pitches[MAX_SLOTS][2];
	uint32_t sizes[MAX_SLOTS];
};

struct frame_msg {
	uint32_t magic;
	uint32_t slot;
	uint64_t seq;          /* KWin's header sequence */
	uint64_t pts_ns;       /* KWin's header pts */
	uint64_t dequeued_ns;  /* CLOCK_MONOTONIC when the process callback ran */
	uint64_t ready_ns;     /* CLOCK_MONOTONIC when the conversion completed */
	uint64_t dropped;      /* frames discarded so far (no free slot / errors) */
};

struct free_msg {
	uint32_t magic;
	uint32_t slot;
};

struct pending {
	uint32_t slot;
	uint64_t seq, pts_ns, dequeued_ns;
};

struct import {
	struct pw_buffer *pwb;
	int fd;
	VASurfaceID surface;
};

static struct {
	/* options */
	int pw_fd, sock_fd, node_id;
	uint32_t width, height, slots;
	uint64_t modifier;
	const char *render_node;

	struct pw_thread_loop *loop;
	struct pw_context *context;
	struct pw_core *core;
	struct pw_stream *stream;
	struct spa_hook stream_listener;
	struct spa_video_info_raw format;
	bool format_fixated;

	VADisplay dpy;
	int drm_fd;
	VAConfigID config;
	VAContextID context_id;
	VASurfaceID out[MAX_SLOTS];
	bool slot_busy[MAX_SLOTS];
	struct import imports[MAX_PW_BUFFERS];

	pthread_mutex_t va_lock;      /* every VA call */
	pthread_mutex_t queue_lock;   /* pending queue and slot_busy */
	pthread_cond_t queue_cond;
	struct pending pending[MAX_SLOTS];
	unsigned pending_head, pending_count;
	bool quit;

	uint64_t frames, dropped, slot_full, import_failures;
	uint64_t last_report_ns;
} S;

static uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static void die(const char *what)
{
	fprintf(stderr, "tabs9-capture: %s\n", what);
	exit(1);
}

/* ---- VA-API ---------------------------------------------------------- */

static void va_setup(void)
{
	int major, minor;
	S.drm_fd = open(S.render_node, O_RDWR | O_CLOEXEC);
	if (S.drm_fd < 0)
		die("cannot open the render node");
	S.dpy = vaGetDisplayDRM(S.drm_fd);
	if (!S.dpy || vaInitialize(S.dpy, &major, &minor) != VA_STATUS_SUCCESS)
		die("vaInitialize failed");
	fprintf(stderr, "tabs9-capture: VA %d.%d %s\n", major, minor, vaQueryVendorString(S.dpy));

	VASurfaceAttrib attr = {
		.type = VASurfaceAttribPixelFormat, .flags = VA_SURFACE_ATTRIB_SETTABLE,
		.value = { .type = VAGenericValueTypeInteger, .value.i = VA_FOURCC_NV12 },
	};
	if (vaCreateSurfaces(S.dpy, VA_RT_FORMAT_YUV420, S.width, S.height, S.out, S.slots,
			     &attr, 1) != VA_STATUS_SUCCESS)
		die("vaCreateSurfaces (NV12 ring) failed");
	if (vaCreateConfig(S.dpy, VAProfileNone, VAEntrypointVideoProc, NULL, 0, &S.config)
	    != VA_STATUS_SUCCESS)
		die("vaCreateConfig (VideoProc) failed");
	if (vaCreateContext(S.dpy, S.config, S.width, S.height, VA_PROGRESSIVE, S.out, S.slots,
			    &S.context_id) != VA_STATUS_SUCCESS)
		die("vaCreateContext failed");
}

/* Export the ring once; the host wraps each fd in a GstMemory for its lifetime. */
static void send_ring(void)
{
	struct ring_msg msg = { .magic = MSG_MAGIC_RING, .slots = S.slots,
				.width = S.width, .height = S.height };
	int fds[MAX_SLOTS];
	for (uint32_t i = 0; i < S.slots; i++) {
		VADRMPRIMESurfaceDescriptor d;
		if (vaExportSurfaceHandle(S.dpy, S.out[i], VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2,
					  VA_EXPORT_SURFACE_READ_WRITE | VA_EXPORT_SURFACE_COMPOSED_LAYERS,
					  &d) != VA_STATUS_SUCCESS)
			die("vaExportSurfaceHandle failed");
		if (d.num_objects != 1 || d.num_layers != 1 || d.layers[0].num_planes != 2)
			die("unexpected NV12 export layout");
		fds[i] = d.objects[0].fd;
		msg.sizes[i] = d.objects[0].size;
		msg.modifier = d.objects[0].drm_format_modifier;
		for (int p = 0; p < 2; p++) {
			msg.offsets[i][p] = d.layers[0].offset[p];
			msg.pitches[i][p] = d.layers[0].pitch[p];
		}
	}
	fprintf(stderr, "tabs9-capture: ring of %u NV12 surfaces, modifier 0x%016llx, pitch %u\n",
		S.slots, (unsigned long long)msg.modifier, msg.pitches[0][0]);

	char control[CMSG_SPACE(sizeof(int) * MAX_SLOTS)];
	struct iovec iov = { .iov_base = &msg, .iov_len = sizeof msg };
	struct msghdr mh = { .msg_iov = &iov, .msg_iovlen = 1,
			     .msg_control = control, .msg_controllen = CMSG_SPACE(sizeof(int) * S.slots) };
	struct cmsghdr *cm = CMSG_FIRSTHDR(&mh);
	cm->cmsg_level = SOL_SOCKET;
	cm->cmsg_type = SCM_RIGHTS;
	cm->cmsg_len = CMSG_LEN(sizeof(int) * S.slots);
	memcpy(CMSG_DATA(cm), fds, sizeof(int) * S.slots);
	if (sendmsg(S.sock_fd, &mh, 0) != (ssize_t)sizeof msg)
		die("sending the ring to the host failed");
	for (uint32_t i = 0; i < S.slots; i++)
		close(fds[i]);
}

static struct import *import_for(struct pw_buffer *pwb)
{
	for (int i = 0; i < MAX_PW_BUFFERS; i++)
		if (S.imports[i].pwb == pwb)
			return &S.imports[i];
	return NULL;
}

/* Import lazily on first use: the stride is only reliable once KWin has
 * filled a chunk.  Cached for the pw_buffer's lifetime (add/remove_buffer),
 * keyed by the buffer, not the fd number. */
static VASurfaceID import_buffer(struct pw_buffer *pwb)
{
	struct import *im = import_for(pwb);
	if (im && im->surface != VA_INVALID_ID)
		return im->surface;
	if (!im) {
		for (int i = 0; i < MAX_PW_BUFFERS; i++)
			if (!S.imports[i].pwb) { im = &S.imports[i]; break; }
		if (!im) return VA_INVALID_ID;
		im->pwb = pwb;
		im->surface = VA_INVALID_ID;
	}
	struct spa_data *d = &pwb->buffer->datas[0];
	if (d->type != SPA_DATA_DmaBuf || d->chunk->stride <= 0)
		return VA_INVALID_ID;
	VADRMPRIMESurfaceDescriptor desc = {
		.fourcc = VA_FOURCC_BGRA, .width = S.width, .height = S.height,
		.num_objects = 1,
		.objects[0] = { .fd = (int)d->fd, .size = d->maxsize,
				.drm_format_modifier = S.format.modifier },
		.num_layers = 1,
		.layers[0] = { .drm_format = DRM_FORMAT_ARGB8888, .num_planes = 1,
			       .object_index = {0}, .offset = { d->chunk->offset },
			       .pitch = { (uint32_t)d->chunk->stride } },
	};
	VASurfaceAttrib attribs[2] = {
		{ .type = VASurfaceAttribMemoryType, .flags = VA_SURFACE_ATTRIB_SETTABLE,
		  .value = { .type = VAGenericValueTypeInteger,
			     .value.i = VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2 } },
		{ .type = VASurfaceAttribExternalBufferDescriptor, .flags = VA_SURFACE_ATTRIB_SETTABLE,
		  .value = { .type = VAGenericValueTypePointer, .value.p = &desc } },
	};
	VASurfaceID surface;
	if (vaCreateSurfaces(S.dpy, VA_RT_FORMAT_RGB32, S.width, S.height, &surface, 1,
			     attribs, 2) != VA_STATUS_SUCCESS) {
		S.import_failures++;
		return VA_INVALID_ID;
	}
	im->fd = (int)d->fd;
	im->surface = surface;
	return surface;
}

static bool convert(VASurfaceID in, VASurfaceID out)
{
	/* Same colour contract as GStreamer's vapostproc on this stream: full
	 * range sRGB in, limited range BT.709 NV12 out.  (The tablet colours
	 * matched the GStreamer path only once the host also pinned
	 * colorimetry=bt709 on the ring's caps; without it the host-side copy
	 * treated the NV12 as something else and bright colours clipped.) */
	VAProcPipelineParameterBuffer p = {
		.surface = in,
		.surface_color_standard = VAProcColorStandardExplicit,
		.output_color_standard = VAProcColorStandardExplicit,
		.output_background_color = 0xff000000,
		.filter_flags = VA_FRAME_PICTURE,
	};
	p.input_color_properties.colour_primaries = 1;          /* BT.709 */
	p.input_color_properties.transfer_characteristics = 13; /* sRGB */
	p.input_color_properties.matrix_coefficients = 0;       /* identity (RGB) */
	p.input_color_properties.color_range = VA_SOURCE_RANGE_FULL;
	p.output_color_properties.colour_primaries = 1;
	p.output_color_properties.transfer_characteristics = 1;
	p.output_color_properties.matrix_coefficients = 1;      /* BT.709 */
	p.output_color_properties.color_range = VA_SOURCE_RANGE_REDUCED;
	VABufferID buf;
	if (vaCreateBuffer(S.dpy, S.context_id, VAProcPipelineParameterBufferType, sizeof p, 1, &p, &buf)
	    != VA_STATUS_SUCCESS)
		return false;
	bool ok = vaBeginPicture(S.dpy, S.context_id, out) == VA_STATUS_SUCCESS &&
		  vaRenderPicture(S.dpy, S.context_id, &buf, 1) == VA_STATUS_SUCCESS &&
		  vaEndPicture(S.dpy, S.context_id) == VA_STATUS_SUCCESS;
	vaDestroyBuffer(S.dpy, buf);
	return ok;
}

/* ---- completion thread: wait for the GPU, then tell the host ------------ */

static void *completion_thread(void *arg)
{
	(void)arg;
	for (;;) {
		struct pending p;
		pthread_mutex_lock(&S.queue_lock);
		while (!S.pending_count && !S.quit)
			pthread_cond_wait(&S.queue_cond, &S.queue_lock);
		if (S.quit) { pthread_mutex_unlock(&S.queue_lock); return NULL; }
		p = S.pending[S.pending_head];
		S.pending_head = (S.pending_head + 1) % MAX_SLOTS;
		S.pending_count--;
		pthread_mutex_unlock(&S.queue_lock);

		/* Poll instead of vaSyncSurface so the VA lock is never held for
		 * the duration of the GPU work (the process callback needs it). */
		for (;;) {
			VASurfaceStatus st = VASurfaceReady;
			pthread_mutex_lock(&S.va_lock);
			vaQuerySurfaceStatus(S.dpy, S.out[p.slot], &st);
			pthread_mutex_unlock(&S.va_lock);
			if (st == VASurfaceReady)
				break;
			usleep(200);
		}
		struct frame_msg m = { .magic = MSG_MAGIC_FRAME, .slot = p.slot, .seq = p.seq,
				       .pts_ns = p.pts_ns, .dequeued_ns = p.dequeued_ns,
				       .ready_ns = now_ns(), .dropped = S.dropped + S.slot_full };
		/* The socket is non-blocking for the reader; wait briefly here. */
		for (int tries = 0; tries < 200; tries++) {
			ssize_t n = write(S.sock_fd, &m, sizeof m);
			if (n == (ssize_t)sizeof m)
				break;
			if (n < 0 && (errno == EAGAIN || errno == EINTR)) { usleep(500); continue; }
			fprintf(stderr, "tabs9-capture: host socket closed\n");
			exit(0);
		}
	}
}

/* ---- PipeWire ---------------------------------------------------------- */

static void on_process(void *data)
{
	(void)data;
	struct pw_buffer *b;
	/* Drain everything queued to us; only the newest frame is converted. */
	struct pw_buffer *last = NULL;
	while ((b = pw_stream_dequeue_buffer(S.stream)) != NULL) {
		if (last) {
			pw_stream_queue_buffer(S.stream, last);
			S.dropped++;
		}
		last = b;
	}
	if (!last)
		return;
	b = last;
	uint64_t dequeued = now_ns();
	struct spa_data *d = &b->buffer->datas[0];
	struct spa_meta_header *h = spa_buffer_find_meta_data(b->buffer, SPA_META_Header, sizeof *h);
	bool corrupted = h && (h->flags & SPA_META_HEADER_FLAG_CORRUPTED);
	int slot = -1;

	if (!corrupted && d->chunk->size != 0) {
		pthread_mutex_lock(&S.queue_lock);
		for (uint32_t i = 0; i < S.slots; i++)
			if (!S.slot_busy[i]) { slot = (int)i; S.slot_busy[i] = true; break; }
		pthread_mutex_unlock(&S.queue_lock);
		if (slot < 0)
			S.slot_full++;
	}
	if (slot >= 0) {
		pthread_mutex_lock(&S.va_lock);
		VASurfaceID in = import_buffer(b);
		bool ok = in != VA_INVALID_ID && convert(in, S.out[slot]);
		pthread_mutex_unlock(&S.va_lock);
		if (!ok) {
			pthread_mutex_lock(&S.queue_lock);
			S.slot_busy[slot] = false;
			pthread_mutex_unlock(&S.queue_lock);
			S.dropped++;
			slot = -1;
		}
	}
	/* Back to KWin in this very cycle.  The GPU may still be reading the
	 * buffer; KWin rotates through its other buffers before reusing this
	 * one and the kernel orders its later write after our read. */
	pw_stream_queue_buffer(S.stream, b);

	if (slot >= 0) {
		S.frames++;
		pthread_mutex_lock(&S.queue_lock);
		struct pending *p = &S.pending[(S.pending_head + S.pending_count) % MAX_SLOTS];
		p->slot = slot;
		p->seq = h ? h->seq : S.frames;
		p->pts_ns = h ? (uint64_t)h->pts : dequeued;
		p->dequeued_ns = dequeued;
		S.pending_count++;
		pthread_cond_signal(&S.queue_cond);
		pthread_mutex_unlock(&S.queue_lock);
	}
	if (dequeued - S.last_report_ns > 5000000000ull) {
		S.last_report_ns = dequeued;
		fprintf(stderr, "tabs9-capture: frames=%llu dropped=%llu slot_full=%llu import_failures=%llu\n",
			(unsigned long long)S.frames, (unsigned long long)S.dropped,
			(unsigned long long)S.slot_full, (unsigned long long)S.import_failures);
	}
}

static void on_remove_buffer(void *data, struct pw_buffer *b)
{
	(void)data;
	struct import *im = import_for(b);
	if (!im)
		return;
	pthread_mutex_lock(&S.va_lock);
	if (im->surface != VA_INVALID_ID)
		vaDestroySurfaces(S.dpy, &im->surface, 1);
	pthread_mutex_unlock(&S.va_lock);
	memset(im, 0, sizeof *im);
}

static void on_add_buffer(void *data, struct pw_buffer *b)
{
	(void)data;
	(void)b;
	/* Imported lazily in on_process once the chunk stride is known. */
}

static struct spa_pod *build_format(struct spa_pod_builder *b, bool fixate)
{
	struct spa_pod_frame f[2];
	spa_pod_builder_push_object(b, &f[0], SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat);
	spa_pod_builder_add(b,
		SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
		SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
		SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRA), 0);
	if (fixate) {
		spa_pod_builder_prop(b, SPA_FORMAT_VIDEO_modifier, SPA_POD_PROP_FLAG_MANDATORY);
		spa_pod_builder_long(b, (int64_t)S.modifier);
	} else {
		spa_pod_builder_prop(b, SPA_FORMAT_VIDEO_modifier,
				     SPA_POD_PROP_FLAG_MANDATORY | SPA_POD_PROP_FLAG_DONT_FIXATE);
		spa_pod_builder_push_choice(b, &f[1], SPA_CHOICE_Enum, 0);
		spa_pod_builder_long(b, (int64_t)S.modifier);
		spa_pod_builder_long(b, (int64_t)S.modifier);
		spa_pod_builder_pop(b, &f[1]);
	}
	spa_pod_builder_add(b,
		SPA_FORMAT_VIDEO_size, SPA_POD_Rectangle(&SPA_RECTANGLE(S.width, S.height)),
		SPA_FORMAT_VIDEO_framerate, SPA_POD_CHOICE_RANGE_Fraction(
			&SPA_FRACTION(0, 1), &SPA_FRACTION(0, 1), &SPA_FRACTION(1000, 1)), 0);
	return spa_pod_builder_pop(b, &f[0]);
}

static void on_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
	(void)data;
	if (param == NULL || id != SPA_PARAM_Format)
		return;
	uint8_t buffer[2048];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof buffer);
	const struct spa_pod *params[4];

	if (spa_format_video_raw_parse(param, &S.format) < 0)
		die("cannot parse the negotiated format");
	const struct spa_pod_prop *mod = spa_pod_find_prop(param, NULL, SPA_FORMAT_VIDEO_modifier);
	if (!mod)
		die("KWin offered no DMA-BUF modifier (memfd path is not supported here)");
	if (mod->flags & SPA_POD_PROP_FLAG_DONT_FIXATE) {
		/* Fixate the modifier ourselves and go again. */
		S.format_fixated = true;
		params[0] = build_format(&b, true);
		pw_stream_update_params(S.stream, params, 1);
		return;
	}
	if (S.format.size.width != S.width || S.format.size.height != S.height)
		die("negotiated size differs from the requested one");
	fprintf(stderr, "tabs9-capture: format %s %ux%u modifier 0x%016llx max %u/%u\n",
		spa_debug_type_find_short_name(spa_type_video_format, S.format.format),
		S.format.size.width, S.format.size.height,
		(unsigned long long)S.format.modifier,
		S.format.max_framerate.num, S.format.max_framerate.denom);

	params[0] = spa_pod_builder_add_object(&b,
		SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
		SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, MAX_PW_BUFFERS),
		SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
		SPA_PARAM_BUFFERS_size, SPA_POD_CHOICE_RANGE_Int(0, 0, INT32_MAX),
		SPA_PARAM_BUFFERS_stride, SPA_POD_CHOICE_RANGE_Int(0, 0, INT32_MAX),
		SPA_PARAM_BUFFERS_align, SPA_POD_Int(16),
		SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int(1 << SPA_DATA_DmaBuf));
	params[1] = spa_pod_builder_add_object(&b,
		SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
		SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
		SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
	params[2] = spa_pod_builder_add_object(&b,
		SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
		SPA_PARAM_META_type, SPA_POD_Id(SPA_META_VideoCrop),
		SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_region)));
	pw_stream_update_params(S.stream, params, 3);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state,
			     const char *error)
{
	(void)data; (void)old;
	fprintf(stderr, "tabs9-capture: stream %s%s%s\n", pw_stream_state_as_string(state),
		error ? ": " : "", error ? error : "");
	if (state == PW_STREAM_STATE_ERROR)
		exit(2);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_state_changed,
	.param_changed = on_param_changed,
	.add_buffer = on_add_buffer,
	.remove_buffer = on_remove_buffer,
	.process = on_process,
};

static void on_core_error(void *data, uint32_t id, int seq, int res, const char *message)
{
	(void)data; (void)seq;
	fprintf(stderr, "tabs9-capture: core error id %u: %s (%s)\n", id, message, spa_strerror(res));
	if (id == PW_ID_CORE && res == -EPIPE)
		exit(2);
}

static const struct pw_core_events core_events = {
	PW_VERSION_CORE_EVENTS,
	.error = on_core_error,
};

/* Slot releases from the host, handled on the PipeWire main loop. */
static void on_socket(void *data, int fd, uint32_t mask)
{
	(void)data;
	if (mask & (SPA_IO_HUP | SPA_IO_ERR)) {
		fprintf(stderr, "tabs9-capture: host went away\n");
		exit(0);
	}
	struct free_msg m;
	ssize_t n;
	while ((n = read(fd, &m, sizeof m)) == (ssize_t)sizeof m) {
		if (m.magic != MSG_MAGIC_FREE || m.slot >= S.slots)
			continue;
		pthread_mutex_lock(&S.queue_lock);
		S.slot_busy[m.slot] = false;
		pthread_mutex_unlock(&S.queue_lock);
	}
	if (n == 0) {
		fprintf(stderr, "tabs9-capture: host closed the socket\n");
		exit(0);
	}
}

int main(int argc, char **argv)
{
	S.pw_fd = 3;
	S.sock_fd = 4;
	S.slots = 6;
	S.modifier = 0x0100000000000009ull; /* I915_FORMAT_MOD_4_TILED */
	S.render_node = "/dev/dri/renderD128";
	static const struct option opts[] = {
		{ "node", required_argument, NULL, 'n' },
		{ "width", required_argument, NULL, 'w' },
		{ "height", required_argument, NULL, 'h' },
		{ "slots", required_argument, NULL, 's' },
		{ "modifier", required_argument, NULL, 'm' },
		{ "render-node", required_argument, NULL, 'r' },
		{ "pw-fd", required_argument, NULL, 'p' },
		{ "sock-fd", required_argument, NULL, 'k' },
		{ 0 }
	};
	int c;
	while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
		switch (c) {
		case 'n': S.node_id = atoi(optarg); break;
		case 'w': S.width = atoi(optarg); break;
		case 'h': S.height = atoi(optarg); break;
		case 's': S.slots = atoi(optarg); break;
		case 'm': S.modifier = strtoull(optarg, NULL, 0); break;
		case 'r': S.render_node = optarg; break;
		case 'p': S.pw_fd = atoi(optarg); break;
		case 'k': S.sock_fd = atoi(optarg); break;
		default: die("usage: --node ID --width W --height H [--slots N] [--modifier M]");
		}
	}
	if (!S.node_id || !S.width || !S.height || S.slots < 2 || S.slots > MAX_SLOTS)
		die("usage: --node ID --width W --height H [--slots 2..8]");
	if (fcntl(S.pw_fd, F_GETFD) < 0 || fcntl(S.sock_fd, F_GETFD) < 0)
		die("--pw-fd / --sock-fd are not open file descriptors");
	pthread_mutex_init(&S.va_lock, NULL);
	pthread_mutex_init(&S.queue_lock, NULL);
	pthread_cond_init(&S.queue_cond, NULL);
	for (int i = 0; i < MAX_PW_BUFFERS; i++)
		S.imports[i].surface = VA_INVALID_ID;

	va_setup();
	send_ring();
	pthread_t thread;
	pthread_create(&thread, NULL, completion_thread, NULL);

	pw_init(&argc, &argv);
	S.loop = pw_thread_loop_new("tabs9-capture", NULL);
	S.context = pw_context_new(pw_thread_loop_get_loop(S.loop), NULL, 0);
	pw_thread_loop_lock(S.loop);
	pw_thread_loop_start(S.loop);
	S.core = pw_context_connect_fd(S.context, fcntl(S.pw_fd, F_DUPFD_CLOEXEC, 5), NULL, 0);
	if (!S.core)
		die("pw_context_connect_fd failed");
	static struct spa_hook core_listener;
	pw_core_add_listener(S.core, &core_listener, &core_events, NULL);

	S.stream = pw_stream_new(S.core, "tabs9-capture", pw_properties_new(
		PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture",
		PW_KEY_MEDIA_ROLE, "Screen", NULL));
	pw_stream_add_listener(S.stream, &S.stream_listener, &stream_events, NULL);

	uint8_t buffer[1024];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buffer, sizeof buffer);
	const struct spa_pod *params[1] = { build_format(&b, false) };
	if (pw_stream_connect(S.stream, PW_DIRECTION_INPUT, S.node_id,
			      PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_RT_PROCESS,
			      params, 1) < 0)
		die("pw_stream_connect failed");
	fcntl(S.sock_fd, F_SETFL, fcntl(S.sock_fd, F_GETFL) | O_NONBLOCK);
	pw_loop_add_io(pw_thread_loop_get_loop(S.loop), S.sock_fd, SPA_IO_IN | SPA_IO_HUP | SPA_IO_ERR,
		       false, on_socket, NULL);
	pw_thread_loop_unlock(S.loop);

	for (;;)
		pause();
}
