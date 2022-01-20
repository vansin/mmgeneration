import numpy as np
import scipy
import torch

from mmgen.ops import filtered_lrelu
from .styleganv2_modules import EqualLinearActModule, ModulatedConv2d


class MappingNetwork(torch.nn.Module):

    def __init__(
        self,
        z_dim,
        c_dim,
        style_channels,
        num_ws,
        num_layers=2,
        lr_multiplier=0.01,
        w_avg_beta=0.998,
    ):
        super().__init__()
        self.z_dim = z_dim
        self.c_dim = c_dim
        self.style_channels = style_channels
        self.num_ws = num_ws
        self.num_layers = num_layers
        self.w_avg_beta = w_avg_beta

        # Construct layers.
        # TODO: check initialization
        self.embed = EqualLinearActModule(
            self.c_dim, self.style_channels) if self.c_dim > 0 else None
        features = [
            self.z_dim + (self.style_channels if self.c_dim > 0 else 0)
        ] + [self.style_channels] * self.num_layers
        for idx, in_features, out_features in zip(
                range(num_layers), features[:-1], features[1:]):
            layer = EqualLinearActModule(
                in_features,
                out_features,
                equalized_lr_cfg=dict(lr_mul=lr_multiplier, gain=1.),
                act_cfg=dict(type='fused_bias'))
            setattr(self, f'fc{idx}', layer)
        self.register_buffer('w_avg', torch.zeros([style_channels]))

    def forward(self,
                z,
                c=None,
                truncation_psi=1,
                truncation_cutoff=None,
                truncation_latent=None,
                update_emas=False):

        if truncation_cutoff is None:
            truncation_cutoff = self.num_ws

        # Embed, normalize, and concatenate inputs.
        x = z.to(torch.float32)
        x = x * (x.square().mean(1, keepdim=True) + 1e-8).rsqrt()
        if self.c_dim > 0:
            y = self.embed(c.to(torch.float32))
            y = y * (y.square().mean(1, keepdim=True) + 1e-8).rsqrt()
            x = torch.cat([x, y], dim=1) if x is not None else y

        # Execute layers.
        for idx in range(self.num_layers):
            x = getattr(self, f'fc{idx}')(x)

        # Update moving average of W.
        if update_emas:
            self.w_avg.copy_(x.detach().mean(dim=0).lerp(
                self.w_avg, self.w_avg_beta))

        # Broadcast and apply truncation.
        x = x.unsqueeze(1).repeat([1, self.num_ws, 1])
        if truncation_psi != 1:
            x[:, :truncation_cutoff] = self.w_avg.lerp(
                x[:, :truncation_cutoff], truncation_psi)
        return x


class SynthesisInput(torch.nn.Module):

    def __init__(self, style_channels, channels, size, sampling_rate,
                 bandwidth):
        super().__init__()
        self.style_channels = style_channels
        self.channels = channels
        self.size = np.broadcast_to(np.asarray(size), [2])
        self.sampling_rate = sampling_rate
        self.bandwidth = bandwidth

        # Draw random frequencies from uniform 2D disc.
        freqs = torch.randn([self.channels, 2])
        radii = freqs.square().sum(dim=1, keepdim=True).sqrt()
        freqs /= radii * radii.square().exp().pow(0.25)
        freqs *= bandwidth
        phases = torch.rand([self.channels]) - 0.5

        # Setup parameters and buffers.
        self.weight = torch.nn.Parameter(
            torch.randn([self.channels, self.channels]))
        # TODO: check initialization
        self.affine = EqualLinearActModule(style_channels, 4)
        self.register_buffer('transform', torch.eye(
            3, 3))  # User-specified inverse transform wrt. resulting image.
        self.register_buffer('freqs', freqs)
        self.register_buffer('phases', phases)

    def forward(self, w):
        # Introduce batch dimension.
        transforms = self.transform.unsqueeze(0)  # [batch, row, col]
        freqs = self.freqs.unsqueeze(0)  # [batch, channel, xy]
        phases = self.phases.unsqueeze(0)  # [batch, channel]

        # Apply learned transformation.
        t = self.affine(w)  # t = (r_c, r_s, t_x, t_y)
        t = t / t[:, :2].norm(
            dim=1, keepdim=True)  # t' = (r'_c, r'_s, t'_x, t'_y)
        m_r = torch.eye(
            3, device=w.device).unsqueeze(0).repeat(
                [w.shape[0], 1, 1])  # Inverse rotation wrt. resulting image.
        m_r[:, 0, 0] = t[:, 0]  # r'_c
        m_r[:, 0, 1] = -t[:, 1]  # r'_s
        m_r[:, 1, 0] = t[:, 1]  # r'_s
        m_r[:, 1, 1] = t[:, 0]  # r'_c
        m_t = torch.eye(
            3, device=w.device).unsqueeze(0).repeat(
                [w.shape[0], 1,
                 1])  # Inverse translation wrt. resulting image.
        m_t[:, 0, 2] = -t[:, 2]  # t'_x
        m_t[:, 1, 2] = -t[:, 3]  # t'_y

        # First rotate resulting image, then translate
        # and finally apply user-specified transform.
        transforms = m_r @ m_t @ transforms

        # Transform frequencies.
        phases = phases + (freqs @ transforms[:, :2, 2:]).squeeze(2)
        freqs = freqs @ transforms[:, :2, :2]

        # Dampen out-of-band frequencies
        # that may occur due to the user-specified transform.
        amplitudes = (1 - (freqs.norm(dim=2) - self.bandwidth) /
                      (self.sampling_rate / 2 - self.bandwidth)).clamp(0, 1)

        # Construct sampling grid.
        theta = torch.eye(2, 3, device=w.device)
        theta[0, 0] = 0.5 * self.size[0] / self.sampling_rate
        theta[1, 1] = 0.5 * self.size[1] / self.sampling_rate
        grids = torch.nn.functional.affine_grid(
            theta.unsqueeze(0), [1, 1, self.size[1], self.size[0]],
            align_corners=False)

        # Compute Fourier features.
        x = (grids.unsqueeze(3) @ freqs.permute(
            0, 2, 1).unsqueeze(1).unsqueeze(2)).squeeze(
                3)  # [batch, height, width, channel]
        x = x + phases.unsqueeze(1).unsqueeze(2)
        x = torch.sin(x * (np.pi * 2))
        x = x * amplitudes.unsqueeze(1).unsqueeze(2)

        # Apply trainable mapping.
        weight = self.weight / np.sqrt(self.channels)
        x = x @ weight.t()

        # Ensure correct shape.
        x = x.permute(0, 3, 1, 2)  # [batch, channel, height, width]
        return x


class SynthesisLayer(torch.nn.Module):

    def __init__(
        self,
        style_channels,
        is_torgb,
        is_critically_sampled,
        use_fp16,
        in_channels,
        out_channels,
        in_size,
        out_size,
        in_sampling_rate,
        out_sampling_rate,
        in_cutoff,
        out_cutoff,
        in_half_width,
        out_half_width,
        conv_kernel=3,
        filter_size=6,
        lrelu_upsampling=2,
        use_radial_filters=False,
        conv_clamp=256,
        magnitude_ema_beta=0.999,
    ):
        super().__init__()
        self.style_channels = style_channels
        self.is_torgb = is_torgb
        self.is_critically_sampled = is_critically_sampled
        self.use_fp16 = use_fp16
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.in_size = np.broadcast_to(np.asarray(in_size), [2])
        self.out_size = np.broadcast_to(np.asarray(out_size), [2])
        self.in_sampling_rate = in_sampling_rate
        self.out_sampling_rate = out_sampling_rate
        self.tmp_sampling_rate = max(in_sampling_rate, out_sampling_rate) * (
            1 if is_torgb else lrelu_upsampling)
        self.in_cutoff = in_cutoff
        self.out_cutoff = out_cutoff
        self.in_half_width = in_half_width
        self.out_half_width = out_half_width
        self.conv_kernel = 1 if is_torgb else conv_kernel
        self.conv_clamp = conv_clamp
        self.magnitude_ema_beta = magnitude_ema_beta

        self.bias = torch.nn.Parameter(torch.zeros([self.out_channels]))

        self.conv = ModulatedConv2d(
            self.in_channels,
            self.out_channels,
            self.conv_kernel,
            self.style_channels,
            demodulate=(not self.is_torgb),
            padding=self.conv_kernel - 1,
        )

        self.register_buffer('magnitude_ema', torch.ones([]))

        # Design upsampling filter.
        self.up_factor = int(
            np.rint(self.tmp_sampling_rate / self.in_sampling_rate))
        assert self.in_sampling_rate * self.up_factor == self.tmp_sampling_rate
        self.up_taps = filter_size * self.up_factor if (
            self.up_factor > 1 and not self.is_torgb) else 1
        self.register_buffer(
            'up_filter',
            self.design_lowpass_filter(
                numtaps=self.up_taps,
                cutoff=self.in_cutoff,
                width=self.in_half_width * 2,
                fs=self.tmp_sampling_rate))

        # Design downsampling filter.
        self.down_factor = int(
            np.rint(self.tmp_sampling_rate / self.out_sampling_rate))
        assert (self.out_sampling_rate *
                self.down_factor == self.tmp_sampling_rate)
        self.down_taps = filter_size * self.down_factor if (
            self.down_factor > 1 and not self.is_torgb) else 1
        self.down_radial = (
            use_radial_filters and not self.is_critically_sampled)
        self.register_buffer(
            'down_filter',
            self.design_lowpass_filter(
                numtaps=self.down_taps,
                cutoff=self.out_cutoff,
                width=self.out_half_width * 2,
                fs=self.tmp_sampling_rate,
                radial=self.down_radial))

        # Compute padding.
        pad_total = (
            self.out_size - 1
        ) * self.down_factor + 1  # Desired output size before downsampling.
        pad_total -= (self.in_size + self.conv_kernel -
                      1) * self.up_factor  # Input size after upsampling.
        # Size reduction caused by the filters.
        pad_total += self.up_taps + self.down_taps - 2
        # Shift sample locations according to
        # the symmetric interpretation (Appendix C.3).
        pad_lo = (pad_total + self.up_factor) // 2
        pad_hi = pad_total - pad_lo
        self.padding = [
            int(pad_lo[0]),
            int(pad_hi[0]),
            int(pad_lo[1]),
            int(pad_hi[1])
        ]

    def forward(self,
                x,
                w,
                noise_mode='random',
                force_fp32=True,
                update_emas=False):
        assert noise_mode in ['random', 'const', 'none']  # unused

        # Track input magnitude.
        if update_emas:
            with torch.autograd.profiler.record_function(
                    'update_magnitude_ema'):
                magnitude_cur = x.detach().to(torch.float32).square().mean()
                self.magnitude_ema.copy_(
                    magnitude_cur.lerp(self.magnitude_ema,
                                       self.magnitude_ema_beta))
        input_gain = self.magnitude_ema.rsqrt()

        # Execute modulated conv2d.
        dtype = torch.float16 if (self.use_fp16 and not force_fp32 and
                                  x.device.type == 'cuda') else torch.float32

        x = self.conv(x, w, input_gain=input_gain)

        # Execute bias, filtered leaky ReLU, and clamping.
        gain = 1 if self.is_torgb else np.sqrt(2)
        slope = 1 if self.is_torgb else 0.2
        x = filtered_lrelu.filtered_lrelu(
            x=x,
            fu=self.up_filter,
            fd=self.down_filter,
            b=self.bias.to(x.dtype),
            up=self.up_factor,
            down=self.down_factor,
            padding=self.padding,
            gain=gain,
            slope=slope,
            clamp=self.conv_clamp)
        assert x.dtype == dtype
        return x

    @staticmethod
    def design_lowpass_filter(numtaps, cutoff, width, fs, radial=False):
        assert numtaps >= 1

        # Identity filter.
        if numtaps == 1:
            return None

        # Separable Kaiser low-pass filter.
        if not radial:
            f = scipy.signal.firwin(
                numtaps=numtaps, cutoff=cutoff, width=width, fs=fs)
            return torch.as_tensor(f, dtype=torch.float32)

        # Radially symmetric jinc-based filter.
        x = (np.arange(numtaps) - (numtaps - 1) / 2) / fs
        r = np.hypot(*np.meshgrid(x, x))
        f = scipy.special.j1(2 * cutoff * (np.pi * r)) / (np.pi * r)
        beta = scipy.signal.kaiser_beta(
            scipy.signal.kaiser_atten(numtaps, width / (fs / 2)))
        w = np.kaiser(numtaps, beta)
        f *= np.outer(w, w)
        f /= np.sum(f)
        return torch.as_tensor(f, dtype=torch.float32)


class SynthesisNetwork(torch.nn.Module):

    def __init__(
        self,
        style_channels,
        out_size,
        img_channels,
        channel_base=32768,
        channel_max=512,
        num_layers=14,
        num_critical=2,
        first_cutoff=2,
        first_stopband=2**2.1,
        last_stopband_rel=2**0.3,
        margin_size=10,
        output_scale=0.25,
        num_fp16_res=4,
        **layer_kwargs,
    ):
        super().__init__()
        self.style_channels = style_channels
        self.num_ws = num_layers + 2
        self.out_size = out_size
        self.img_channels = img_channels
        self.num_layers = num_layers
        self.num_critical = num_critical
        self.margin_size = margin_size
        self.output_scale = output_scale
        self.num_fp16_res = num_fp16_res

        # Geometric progression of layer cutoffs and min. stopbands.
        last_cutoff = self.out_size / 2  # f_{c,N}
        last_stopband = last_cutoff * last_stopband_rel  # f_{t,N}
        exponents = np.minimum(
            np.arange(self.num_layers + 1) /
            (self.num_layers - self.num_critical), 1)
        cutoffs = first_cutoff * (last_cutoff /
                                  first_cutoff)**exponents  # f_c[i]
        stopbands = first_stopband * (last_stopband /
                                      first_stopband)**exponents  # f_t[i]

        # Compute remaining layer parameters.
        sampling_rates = np.exp2(
            np.ceil(np.log2(np.minimum(stopbands * 2, self.out_size))))  # s[i]
        half_widths = np.maximum(stopbands,
                                 sampling_rates / 2) - cutoffs  # f_h[i]
        sizes = sampling_rates + self.margin_size * 2
        sizes[-2:] = self.out_size
        channels = np.rint(
            np.minimum((channel_base / 2) / cutoffs, channel_max))
        channels[-1] = self.img_channels

        # Construct layers.
        self.input = SynthesisInput(
            style_channels=self.style_channels,
            channels=int(channels[0]),
            size=int(sizes[0]),
            sampling_rate=sampling_rates[0],
            bandwidth=cutoffs[0])
        self.layer_names = []
        for idx in range(self.num_layers + 1):
            prev = max(idx - 1, 0)
            is_torgb = (idx == self.num_layers)
            is_critically_sampled = (
                idx >= self.num_layers - self.num_critical)
            use_fp16 = (
                sampling_rates[idx] * (2**self.num_fp16_res) > self.out_size)
            layer = SynthesisLayer(
                style_channels=self.style_channels,
                is_torgb=is_torgb,
                is_critically_sampled=is_critically_sampled,
                use_fp16=use_fp16,
                in_channels=int(channels[prev]),
                out_channels=int(channels[idx]),
                in_size=int(sizes[prev]),
                out_size=int(sizes[idx]),
                in_sampling_rate=int(sampling_rates[prev]),
                out_sampling_rate=int(sampling_rates[idx]),
                in_cutoff=cutoffs[prev],
                out_cutoff=cutoffs[idx],
                in_half_width=half_widths[prev],
                out_half_width=half_widths[idx],
                **layer_kwargs)
            name = f'L{idx}_{layer.out_size[0]}_{layer.out_channels}'
            setattr(self, name, layer)
            self.layer_names.append(name)

    def forward(self, ws, **layer_kwargs):
        ws = ws.to(torch.float32).unbind(dim=1)

        # Execute layers.
        x = self.input(ws[0])
        for name, w in zip(self.layer_names, ws[1:]):
            x = getattr(self, name)(x, w, **layer_kwargs)
        if self.output_scale != 1:
            x = x * self.output_scale

        x = x.to(torch.float32)
        return x
