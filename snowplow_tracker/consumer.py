"""
    consumer.py

    Copyright (c) 2013-2014 Snowplow Analytics Ltd. All rights reserved.

    This program is licensed to you under the Apache License Version 2.0,
    and you may not use this file except in compliance with the Apache License
    Version 2.0. You may obtain a copy of the Apache License Version 2.0 at
    http://www.apache.org/licenses/LICENSE-2.0.

    Unless required by applicable law or agreed to in writing,
    software distributed under the Apache License Version 2.0 is distributed on
    an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
    express or implied. See the Apache License Version 2.0 for the specific
    language governing permissions and limitations there under.

    Authors: Anuj More, Alex Dean, Fred Blundun
    Copyright: Copyright (c) 2013-2014 Snowplow Analytics Ltd
    License: Apache License Version 2.0
"""

import requests
import json
import threading
import celery
from celery import Celery
from celery.contrib.methods import task
import redis
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

DEFAULT_MAX_LENGTH = 10

try:
    # Check whether a custom Celery configuration module named "snowplow_celery_config" exists
    import snowplow_celery_config
    app = Celery()
    app.config_from_object(snowplow_celery_config)

except ImportError:
    # Otherwise configure Celery with default settings
    app = Celery("Snowplow", broker="redis://guest@localhost//")


class Consumer(object):
    """
        Synchronously send Snowplow events to a Snowplow as_collector_uri
        Supports both GET and POST requests
    """

    def __init__(self, endpoint, method="http-get", buffer_size=None, on_success=None, on_failure=None):

        self.endpoint = Consumer.as_collector_uri(endpoint)

        self.method = method

        if buffer_size is None:
            if method == "http-post":
                buffer_size = DEFAULT_MAX_LENGTH
            else:
                buffer_size = 1
        self.buffer_size = buffer_size
        self.buffer = []

        self.on_success = on_success
        self.on_failure = on_failure

    @staticmethod
    def as_collector_uri(endpoint):
        return "http://" + endpoint + "/i"

    def input(self, payload):
        """
            Adds an event to the buffer.
            If the maximum size has been reached, flushes the buffer.
        """
        self.buffer.append(payload)
        if len(self.buffer) >= self.buffer_size:
            return self.flush()

    @task(name="Flush")
    def flush(self):
        """
            Sends all events in the buffer to the collector.
        """

        if self.method == "http-post":
            data = json.dumps(self.buffer)
            buffer_length = len(self.buffer)
            self.buffer = []
            status_code = self.http_post(data).status_code
            if status_code == 200 and self.on_success is not None:
                self.on_success(buffer_length)
            elif self.on_failure is not None:
                self.on_failure(0, data)
            return status_code

        elif self.method == "http-get":
            success_count = 0
            unsent_requests = []

            while len(self.buffer) > 0:
                payload = self.buffer.pop()
                status_code = self.http_get(payload).status_code
                if status_code == 200:
                    success_count += 1
                else:
                    unsent_requests.append(payload)

            if len(unsent_requests) == 0 and self.on_success is not None:
                self.on_success(success_count)
            elif self.on_failure is not None:
                self.on_failure(success_count, unsent_requests)

            return status_code

        else:
            logger.warn(self.method + ' is not a recognised HTTP method. Use "http-get" or "http-post".')

    def http_post(self, data):
        logger.debug("Sending POST request...")
        r = requests.post(self.endpoint, data=data)
        logger.debug("POST request finished with status code: " + str(r.status_code))
        return r

    def http_get(self, payload):
        logger.debug("Sending GET request...")
        r = requests.get(self.endpoint, params=payload)        
        logger.debug("GET request finished with status code: " + str(r.status_code))
        return r

    def sync_flush(self):
        """
            Calls the flush method of the base Consumer class.
            This is guaranteed to be blocking, not asynchronous.
        """
        logger.debug("Starting synchronous flush...")
        result = Consumer.flush(self)
        logger.debug("Finished synchrous flush")
        return result

class AsyncConsumer(Consumer):
    """
        Uses threads to send HTTP requests asynchronously
    """
    def flush(self):
        logger.debug("Flushing thread running...")
        t = threading.Thread(target=super(AsyncConsumer, self).flush)
        t.start()


class CeleryConsumer(Consumer):
    """
        Uses a Celery worker to send HTTP requests asynchronously
    """
    def flush(self):
        super(CeleryConsumer, self).flush.delay()


class RedisConsumer(object):
    """
        Sends Snowplow events to a Redis database
    """
    def __init__(self, rdb=None, key="snowplow"):
        if rdb is None:
            rdb = redis.StrictRedis()
        self.rdb = rdb
        self.key = key

    def input(self, payload):
        logger.debug("Pushing event to Redis queue...")
        self.rdb.rpush(self.key, json.dumps(payload))
        logger.debug("Finished sending event to Redis.")

    def flush(self):
        logger.warn("The RedisConsumer class does not need to be flushed")

    def sync_flush(self):
        self.flush()
