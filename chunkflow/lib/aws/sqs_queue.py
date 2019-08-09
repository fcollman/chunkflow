import boto3
import hashlib
from time import sleep
from cloudvolume.secrets import aws_credentials


class SQSQueue(object):
    """upload/fetch messages using AWS Simple Queue Services."""

    def __init__(self,
                 queue_name: str,
                 visibility_timeout: int = None,
                 wait_if_empty: int = 100,
                 fetch_wait_time_seconds: int = 20):
        """
        Parameters
        ------------
        visibility_timeout: 
            make the task invisible for a while (seconds)
        wait_if_empty: 
            wait for a while and continue fetching task if the queue is empty.
        fetch_wait_time_seconds: 
            the maximum wait time if the fetched queue is empty. 
            The maximum value is 20, which will use the long polling. If we set it to be 0,
            the message fetch could fail even if the queue has messages. This problem is due 
            to the fact that the message in queue is managed distributedly, and the query was
            only sent to a few servers. Normally, we should set fetch wait time to use long poll. 
            checkout the AWS `documentation <https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-long-polling.html#sqs-short-long-polling-differences>`_
        """
        credentials = aws_credentials()
        self.client = boto3.client(
            'sqs',
            region_name=credentials['AWS_DEFAULT_REGION'],
            aws_secret_access_key=credentials['AWS_SECRET_ACCESS_KEY'],
            aws_access_key_id=credentials['AWS_ACCESS_KEY_ID'])

        resp = self.client.get_queue_url(QueueName=queue_name)
        self.queue_url = resp['QueueUrl']
        self.visibility_timeout = visibility_timeout
        self.wait_if_empty = wait_if_empty
        self.fetch_wait_time_seconds = fetch_wait_time_seconds

    def __iter__(self):
        return self

    def _receive_message(self):
        if self.visibility_timeout:
            resp = self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                MessageAttributeNames=['All'],
                VisibilityTimeout=self.visibility_timeout,
                # we should set this wait time to use long poll
                # checkout the AWS documentation here:
                # https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-long-polling.html#sqs-short-long-polling-differences
                WaitTimeSeconds=self.fetch_wait_time_seconds)
        else:
            # use the visibility timeout in the queue
            resp = self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                MessageAttributeNames=['All'],
                WaitTimeSeconds=self.fetch_wait_time_seconds)
        return resp

    def __next__(self):
        resp = self._receive_message()
        if 'Messages' not in resp:
            # the queue is empty
            if self.wait_if_empty:
                # the 20 seconds additional waiting time is from the receiving
                print('the queue is empty, wait for {} seconds'.format(
                    self.wait_if_empty + 20))
                sleep(self.wait_if_empty)
                # contine trying to receive message
                return self.__next__()
            else:
                raise StopIteration
        else:
            receipt_handle = resp['Messages'][0]['ReceiptHandle']
            body = resp['Messages'][0]['Body']
            md5_of_body = resp['Messages'][0]['MD5OfBody']
            assert md5_of_body == hashlib.md5(body.encode('utf-8')).hexdigest()
            assert isinstance(receipt_handle, str)
            assert body is not None
            return receipt_handle, body

    def delete(self, receipt_handle: str):
        """
        Parameters
        -----------
        receipt_handle:
            a random string as a handle of the message in queue.
        """
        self.client.delete_message(
            QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    def _send_entry_list(self, entry_list: list):
        resp = self.client.send_message_batch(
            QueueUrl=self.queue_url, Entries=entry_list)
        # the failed list should be empty
        assert 'Failed' not in resp
        entry_list.clear()

    def send_message_list(self, message_list: list):
        '''
        Use batch mode to send a bunch of messages quickly.

        Parameters
        -----------
        message_list: 
            a list of input messages. the messages are string.
        '''
        # the maximum number in a batch is 10
        task_entries = []
        for message in message_list:
            entry = {'Id': message, 'MessageBody': message}
            task_entries.append(entry)

            # use batch mode to produce tasks
            if len(task_entries) == 10:
                self._send_entry_list(task_entries)

        # send the remaining tasks less than 10
        if task_entries:
            self._send_entry_list(task_entries)

        # make sure that the remaining task list is empty
        assert not task_entries