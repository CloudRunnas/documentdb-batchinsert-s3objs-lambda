# AWS Lambda container image (Python)
FROM public.ecr.aws/lambda/python:3.11

COPY function ${LAMBDA_TASK_ROOT}/function

RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/function/requirements.txt \
    && curl -fsSL -o /var/task/global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

CMD [ "function.main.handler" ]
