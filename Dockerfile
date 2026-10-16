# CodeArts2API（Python 增强版）
#
# 为什么用 slim 而不是 alpine：cryptography 在 musl 上要现编译，镜像构建慢且容易失败；
# slim（glibc）可以直接装官方 wheel。
FROM python:3.12-slim

# 容器里没有 tzdata 时日志时间会是 UTC，排查问题很别扭；顺便装上 ca-certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷代码：改代码时不用重装依赖，构建缓存能命中
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY server.py benefit.py login.py ./
COPY config.example.json ./

# 凭证与状态放在卷里（compose 会挂载），镜像里不预置任何账号
RUN mkdir -p /app/auths /app/data \
    && useradd --create-home --shell /usr/sbin/nologin ca2a \
    && chown -R ca2a:ca2a /app
USER ca2a

# 容器内必须监听 0.0.0.0，否则宿主机的端口映射打不进来。
# 对外暴露面靠 compose 的 "127.0.0.1:8787:8787" 限制在回环地址上。
ENV CA2A_LISTEN_HOST=0.0.0.0 \
    CA2A_LISTEN_PORT=8787 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8787

# 健康检查用 /healthz（不鉴权），便于 compose/k8s 判断存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status==200 else 1)"

CMD ["python", "server.py"]
