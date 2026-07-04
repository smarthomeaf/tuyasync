ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 PYTHONUNBUFFERED=1

# System deps: python + build basics for tinytuya's crypto
RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# App code
COPY rootfs/app /app

# run.sh is the entrypoint (installed to rootfs root)
COPY rootfs/run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
