FROM python:3.13-slim
WORKDIR /app
COPY live.py .
# Runs as a normal user; the data volume is the only place it writes.
RUN useradd --uid 10001 live && mkdir /livedata && chown live /livedata
USER live
EXPOSE 8090
CMD ["python", "live.py"]
