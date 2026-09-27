FROM python:3.12-alpine

WORKDIR /app
COPY server.py tplparse.py sqldump.py ./
COPY static ./static

ENV SHM_TOPO_HOST=0.0.0.0 \
    SHM_TOPO_PORT=8765 \
    SHM_TOPO_DB=/data/topology.db \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8765/api/topologies >/dev/null || exit 1

CMD ["python3", "server.py"]
