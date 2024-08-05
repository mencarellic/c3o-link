from aws_lambda_powertools.event_handler import (APIGatewayRestResolver, Response)
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools import Logger
from aws_lambda_powertools import Tracer
from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit
import boto3
from botocore.exceptions import ClientError
import pandas as pd
from thefuzz import fuzz
from thefuzz import process

app = APIGatewayRestResolver()
tracer = Tracer()
logger = Logger()
metrics = Metrics(namespace="Powertools")

defaultRedirect = "https://www.google.com"

client = boto3.client('dynamodb')

@tracer.capture_method
def read_csv(file_path="paths.csv"):
    df = pd.read_csv(file_path,sep=',',header=0, names=['path', 'redirectDestination'])

    return df.to_dict('records')

@tracer.capture_method
def fuzzy_search(path):
    logger.info("Getting all items from DynamoDB for fuzzy search")
    data = client.scan(TableName='C3OLink')

    logger.info("Iterating over items")
    for item in data['Items']:
        if fuzz.ratio(item['path']['S'], path) > 80:
            logger.info(f"Fuzzy match found - {item['path']['S']} - {path}")
            return item['redirectDestination']['S']

    return defaultRedirect

@tracer.capture_method
def sync_dynamodb(df):
    logger.info("Syncing CSV to DynamoDB")

    # Reset counters
    added = 0
    updated = 0
    skipped = 0

    for record in df:
        try:
            logger.info(f"Adding item to DynamoDB - {record['path']}")
            response = client.put_item(
                TableName='C3OLink',
                Item={
                    'path': {
                        'S': record['path']
                    },
                    'redirectDestination': {
                        'S': record['redirectDestination']
                    }
                }
            )
            logger.info(f"Added item to DynamoDB - {record['path']}")
            added += 1
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                logger.info(f"Item already exists in DynamoDB - Updating {record['path']}")
                client.update_item(
                    TableName='C3OLink',
                    Key={'path': record['path']},
                    UpdateExpression='SET #attr = :val',
                    ExpressionAttributeNames={'#attr': 'redirectDestination'},
                    ExpressionAttributeValues={':val': record['redirectDestination']}
                )
                logger.info(f"Updated item in DynamoDB - {record['path']}")
                updated += 1
            else:
                logger.error(f"Error processing item: {record}. Error: {e}")
                skipped += 1

    if skipped > 0:
        return Response(
                status_code=500,
                body=f"Error processing {skipped} items"
            )
    else:
        return Response(
                status_code=200,
                body=f"Added: {added}, Updated: {updated}, Skipped: {skipped}"
            )

    logger.info("Sync complete")


@tracer.capture_method
def fetch_redirect(path):
    logger.info(f"Fetching DynamoDB value for path - {path}")

    try:
        response = client.get_item(
            TableName='C3OLink',
            Key={
                'path': {
                    'S': path
                }
            }
        )
    except Exception as e:
        logger.exception(f"Error fetching path from DynamoDB - {e}")
        return defaultRedirect

    logger.info(f"Response: {response}")

    if response.get('Item'):
        redirect_destination = response.get('Item').get('redirectDestination').get('S', defaultRedirect)
        logger.info(f"Forwarding to: {redirect_destination}")
    else:
        logger.info(f"Path not found in DynamoDB - {path} - Trying Fuzzy Search")
        redirect_destination = fuzzy_search(path)

    return redirect_destination


@app.get("/.+")
@tracer.capture_method
def catch_any_route_get_method():
    metrics.add_metric(name="RedirectPathInvocation", unit=MetricUnit.Count, value=1)
    logger.info(f"RedirectPathInvocation - HTTP 302 - {app.current_event.path}")

    path = app.current_event.path.strip("/")
    logger.info(f"Path: {path}")
    redirect = fetch_redirect(path)

    return Response(
        status_code=302,
        headers={
            "Location": redirect
        },
        body=""
    )

@app.get("/")
@tracer.capture_method
def base():
    metrics.add_metric(name="BasePathInvocation", unit=MetricUnit.Count, value=1)
    logger.info("BasePathInvocation")

    refresh = app.current_event.get_header_value(name="x-refresh", case_sensitive=False, default_value="false")
    logger.info(f"Refresh?: {refresh}")

    if refresh.lower() == "true":
        df = read_csv()
        sync_dynamodb(df)
    else:
        return Response(
            status_code=200
        )

@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
