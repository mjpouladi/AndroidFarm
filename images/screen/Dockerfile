FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 HOME=/home/screen
RUN apt-get update && apt-get install -y --no-install-recommends \
    scrcpy adb xvfb x11vnc novnc websockify curl ca-certificates python3 procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 screen
COPY images/screen/start.py /start.py
COPY images/screen/index.html /usr/share/novnc/index.html
USER 10001:10001
EXPOSE 6080
CMD ["python3", "/start.py"]
