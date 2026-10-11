# Geospatial Lambda image (x86_64): field analyzer, baselines, hail rasters, overlays.
# Built from the repo root by infra/stacks/common.py geo_lambda(); each function sets its own handler (cmd).
FROM public.ecr.aws/lambda/python:3.12

# The Amazon Linux 2023 base image lacks libexpat, which the GDAL bundled in the geo wheels loads.
RUN dnf install -y expat && dnf clean all

COPY infra/docker/geo/requirements.txt /tmp/geo-requirements.txt
RUN pip install --no-cache-dir -r /tmp/geo-requirements.txt

COPY agent/fieldwatch ${LAMBDA_TASK_ROOT}/fieldwatch
COPY agent/cloud ${LAMBDA_TASK_ROOT}/cloud
COPY agent/catalog ${LAMBDA_TASK_ROOT}/catalog
COPY agent/fixtures/contracts ${LAMBDA_TASK_ROOT}/fixtures/contracts

ENV GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.TIF,.gz,.grib2 \
    AWS_NO_SIGN_REQUEST=YES
