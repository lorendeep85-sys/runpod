# RunPod Serverless — TsukiHime H.264 encode worker.
#
# Pola WAJIB RunPod Serverless: SDK + handler dibakar ke image, dijalankan lewat CMD.
# (dockerStartCmd di template TIDAK dieksekusi untuk serverless — sudah diuji.)
FROM python:3.11-slim

# KRUSIAL untuk NVENC: tanpa dua env ini, nvidia-container-runtime tidak me-mount
# libnvidia-encode dari host, sehingga h264_nvenc gagal dan jatuh ke CPU.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

ARG FFURL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2025-06-30-13-55/ffmpeg-N-120061-gcfd1f81e7d-linux64-gpl.tar.xz

RUN apt-get update && apt-get install -y --no-install-recommends \
      par2 wget xz-utils ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# ffmpeg static BtbN build 2025-06 — dipin karena build 'latest' butuh driver 610+
# (NVENC API 13.1) sementara GPU cloud umumnya masih 580 (API 13.0).
RUN wget -q "$FFURL" -O /tmp/ff.tar.xz && \
    mkdir -p /opt/ffbin && \
    tar xf /tmp/ff.tar.xz -C /opt/ffbin --strip-components=2 --wildcards '*/bin/*' && \
    rm /tmp/ff.tar.xz

ENV FFMPEG=/opt/ffbin/ffmpeg FFPROBE=/opt/ffbin/ffprobe

RUN pip install --no-cache-dir runpod boto3 sabyenc3

WORKDIR /app
COPY handler.py nzb_fetch.py release_picker.py /app/

CMD ["python", "-u", "/app/handler.py"]
