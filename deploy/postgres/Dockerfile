# 自建 PG 镜像：pgvector + zhparser
#
# 为什么必须自建：官方 pgvector/pgvector 镜像不含 zhparser，
# 而 docs/03 §3.2 的 DDL 里 `to_tsvector('zh', content)` 依赖它。
# 这一项在 docs/05 §四.3 被列为"环境落地的第一个硬任务"。
#
# 构建：cd deploy && docker compose build postgres
# 国内网络提示：如果 github 拉不动，把 ZHPARSER_URL 改成 gitee 镜像，
#   例如 https://gitee.com/mirrors_amutu/zhparser/repository/archive/V2.2.tar.gz
#   或提前把源码包放到本目录，改用 COPY 引入。

FROM pgvector/pgvector:pg16

ARG PG_MAJOR=16
ARG SCWS_VER=1.2.3
ARG ZHPARSER_VER=V2.2
ARG SCWS_URL=http://www.xunsearch.com/scws/down/scws-${SCWS_VER}.tar.bz2
ARG ZHPARSER_URL=https://github.com/amutu/zhparser/archive/refs/tags/${ZHPARSER_VER}.tar.gz

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-${PG_MAJOR} \
        wget ca-certificates bzip2; \
    # ── 1. 编译 SCWS 中文分词库（zhparser 的依赖）──
    cd /tmp; \
    wget -q "$SCWS_URL"; \
    tar xjf "scws-${SCWS_VER}.tar.bz2"; \
    cd "scws-${SCWS_VER}"; \
    ./configure --prefix=/usr/local; \
    make -j"$(nproc)"; \
    make install; \
    # ── 2. 编译 zhparser 扩展 ──
    cd /tmp; \
    wget -q "$ZHPARSER_URL" -O zhparser.tar.gz; \
    tar xzf zhparser.tar.gz; \
    cd "zhparser-${ZHPARSER_VER#V}"; \
    SCWS_HOME=/usr/local make -j"$(nproc)"; \
    SCWS_HOME=/usr/local make install; \
    # ── 3. 清理编译环境，控制镜像体积 ──
    apt-get purge -y --auto-remove build-essential postgresql-server-dev-${PG_MAJOR} wget bzip2; \
    rm -rf /tmp/* /var/lib/apt/lists/*

# 构建期自检：确保 .so 真的落到了 PG 的库目录
RUN set -eux; \
    test -f /usr/lib/postgresql/${PG_MAJOR}/lib/zhparser.so; \
    test -f /usr/share/postgresql/${PG_MAJOR}/extension/zhparser.control; \
    ls -lh /usr/lib/postgresql/${PG_MAJOR}/lib/zhparser.so

LABEL org.opencontainers.image.description="PostgreSQL ${PG_MAJOR} + pgvector + zhparser (中文全文检索)"
