FROM python:3.11-slim

# 用国内 apt 镜像加速
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    gcc libyaml-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple flask

# 共享模块 + 三个入口
COPY wkm_common.py web-keyword-monitor.py web-keyword-monitor-ui.py web-keyword-monitor-web.py ./

VOLUME /app/config
VOLUME /app/data

ENV CONFIG=/app/config/config.yaml

CMD ["python3", "web-keyword-monitor.py", "--config", "/app/config/config.yaml", "--interval", "600"]
