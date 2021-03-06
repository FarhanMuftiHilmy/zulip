
import os
import time
import ujson
import smtplib

from django.conf import settings
from django.http import HttpResponse
from django.test import TestCase
from mock import patch, MagicMock
from typing import Any, Callable, Dict, List, Mapping, Tuple

from zerver.lib.test_helpers import simulated_queue_client
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import get_client, UserActivity, PreregistrationUser
from zerver.worker import queue_processors
from zerver.worker.queue_processors import (
    get_active_worker_queues,
    QueueProcessingWorker,
    LoopQueueProcessingWorker,
    MissedMessageWorker,
)

Event = Dict[str, Any]

# This is used for testing LoopQueueProcessingWorker, which
# would run forever if we don't mock time.sleep to abort the
# loop.
class AbortLoop(Exception):
    pass

class WorkerTest(ZulipTestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.consumers = {}  # type: Dict[str, Callable[[Dict[str, Any]], None]]
            self.queue = []  # type: List[Tuple[str, Dict[str, Any]]]

        def register_json_consumer(self,
                                   queue_name: str,
                                   callback: Callable[[Dict[str, Any]], None]) -> None:
            self.consumers[queue_name] = callback

        def start_consuming(self) -> None:
            for queue_name, data in self.queue:
                callback = self.consumers[queue_name]
                callback(data)

        def drain_queue(self, queue_name: str, json: bool) -> List[Event]:
            assert json
            events = [
                dct
                for (queue_name, dct)
                in self.queue
            ]

            # IMPORTANT!
            # This next line prevents us from double draining
            # queues, which was a bug at one point.
            self.queue = []

            return events

    def test_missed_message_worker(self) -> None:
        cordelia = self.example_user('cordelia')
        hamlet = self.example_user('hamlet')
        othello = self.example_user('othello')

        hamlet1_msg_id = self.send_personal_message(
            from_email=cordelia.email,
            to_email=hamlet.email,
            content='hi hamlet',
        )

        hamlet2_msg_id = self.send_personal_message(
            from_email=cordelia.email,
            to_email=hamlet.email,
            content='goodbye hamlet',
        )

        othello_msg_id = self.send_personal_message(
            from_email=cordelia.email,
            to_email=othello.email,
            content='where art thou, othello?',
        )

        events = [
            dict(user_profile_id=hamlet.id, message_id=hamlet1_msg_id),
            dict(user_profile_id=hamlet.id, message_id=hamlet2_msg_id),
            dict(user_profile_id=othello.id, message_id=othello_msg_id),
        ]

        fake_client = self.FakeClient()
        for event in events:
            fake_client.queue.append(('missedmessage_emails', event))

        mmw = MissedMessageWorker()

        time_mock = patch(
            'zerver.worker.queue_processors.time.sleep',
            side_effect=AbortLoop,
        )

        send_mock = patch(
            'zerver.lib.notifications.do_send_missedmessage_events_reply_in_zulip'
        )

        with send_mock as sm, time_mock as tm:
            with simulated_queue_client(lambda: fake_client):
                try:
                    mmw.setup()
                    mmw.start()
                except AbortLoop:
                    pass

        self.assertEqual(tm.call_args[0][0], 120)  # should sleep two minutes

        args = [c[0] for c in sm.call_args_list]
        arg_dict = {
            arg[0].id: dict(
                missed_messages=arg[1],
                count=arg[2],
            )
            for arg in args
        }

        hamlet_info = arg_dict[hamlet.id]
        self.assertEqual(hamlet_info['count'], 2)
        self.assertEqual(
            {m.content for m in hamlet_info['missed_messages']},
            {'hi hamlet', 'goodbye hamlet'},
        )

        othello_info = arg_dict[othello.id]
        self.assertEqual(othello_info['count'], 1)
        self.assertEqual(
            {m.content for m in othello_info['missed_messages']},
            {'where art thou, othello?'}
        )

    def test_mirror_worker(self) -> None:
        fake_client = self.FakeClient()
        data = [
            dict(
                message=u'\xf3test',
                time=time.time(),
                rcpt_to=self.example_email('hamlet'),
            ),
            dict(
                message='\xf3test',
                time=time.time(),
                rcpt_to=self.example_email('hamlet'),
            ),
            dict(
                message='test',
                time=time.time(),
                rcpt_to=self.example_email('hamlet'),
            ),
        ]
        for element in data:
            fake_client.queue.append(('email_mirror', element))

        with patch('zerver.worker.queue_processors.mirror_email'):
            with simulated_queue_client(lambda: fake_client):
                worker = queue_processors.MirrorWorker()
                worker.setup()
                worker.start()

    def test_email_sending_worker_retries(self) -> None:
        """Tests the retry_send_email_failures decorator to make sure it
        retries sending the email 3 times and then gives up."""
        fake_client = self.FakeClient()

        data = {'test': 'test', 'id': 'test_missed'}
        fake_client.queue.append(('missedmessage_email_senders', data))

        def fake_publish(queue_name: str,
                         event: Dict[str, Any],
                         processor: Callable[[Any], None]) -> None:
            fake_client.queue.append((queue_name, event))

        with simulated_queue_client(lambda: fake_client):
            worker = queue_processors.MissedMessageSendingWorker()
            worker.setup()
            with patch('zerver.worker.queue_processors.send_email_from_dict',
                       side_effect=smtplib.SMTPServerDisconnected), \
                    patch('zerver.lib.queue.queue_json_publish',
                          side_effect=fake_publish), \
                    patch('logging.exception'):
                worker.start()

        self.assertEqual(data['failed_tries'], 4)

    def test_signups_worker_retries(self) -> None:
        """Tests the retry logic of signups queue."""
        fake_client = self.FakeClient()

        user_id = self.example_user('hamlet').id
        data = {'user_id': user_id, 'id': 'test_missed'}
        fake_client.queue.append(('signups', data))

        def fake_publish(queue_name: str, event: Dict[str, Any], processor: Callable[[Any], None]) -> None:
            fake_client.queue.append((queue_name, event))

        fake_response = MagicMock()
        fake_response.status_code = 400
        fake_response.text = ujson.dumps({'title': ''})
        with simulated_queue_client(lambda: fake_client):
            worker = queue_processors.SignupWorker()
            worker.setup()
            with patch('zerver.worker.queue_processors.requests.post',
                       return_value=fake_response), \
                    patch('zerver.lib.queue.queue_json_publish',
                          side_effect=fake_publish), \
                    patch('logging.info'), \
                    self.settings(MAILCHIMP_API_KEY='one-two',
                                  PRODUCTION=True,
                                  ZULIP_FRIENDS_LIST_ID='id'):
                worker.start()

        self.assertEqual(data['failed_tries'], 4)

    def test_invites_worker(self) -> None:
        fake_client = self.FakeClient()
        invitor = self.example_user('iago')
        prereg_alice = PreregistrationUser.objects.create(
            email=self.nonreg_email('alice'), referred_by=invitor, realm=invitor.realm)
        PreregistrationUser.objects.create(
            email=self.nonreg_email('bob'), referred_by=invitor, realm=invitor.realm)
        data = [
            dict(prereg_id=prereg_alice.id, referrer_id=invitor.id, email_body=None),
            # Nonexistent prereg_id, as if the invitation was deleted
            dict(prereg_id=-1, referrer_id=invitor.id, email_body=None),
            # Form with `email` is from versions up to Zulip 1.7.1
            dict(email=self.nonreg_email('bob'), referrer_id=invitor.id, email_body=None),
        ]
        for element in data:
            fake_client.queue.append(('invites', element))

        with simulated_queue_client(lambda: fake_client):
            worker = queue_processors.ConfirmationEmailWorker()
            worker.setup()
            with patch('zerver.worker.queue_processors.do_send_confirmation_email'), \
                    patch('zerver.worker.queue_processors.create_confirmation_link'), \
                    patch('zerver.worker.queue_processors.send_future_email') \
                    as send_mock, \
                    patch('logging.info'):
                worker.start()
                self.assertEqual(send_mock.call_count, 2)

    def test_UserActivityWorker(self) -> None:
        fake_client = self.FakeClient()

        user = self.example_user('hamlet')
        UserActivity.objects.filter(
            user_profile = user.id,
            client = get_client('ios')
        ).delete()

        data = dict(
            user_profile_id = user.id,
            client = 'ios',
            time = time.time(),
            query = 'send_message'
        )
        fake_client.queue.append(('user_activity', data))

        with simulated_queue_client(lambda: fake_client):
            worker = queue_processors.UserActivityWorker()
            worker.setup()
            worker.start()
            activity_records = UserActivity.objects.filter(
                user_profile = user.id,
                client = get_client('ios')
            )
            self.assertTrue(len(activity_records), 1)
            self.assertTrue(activity_records[0].count, 1)

    def test_error_handling(self) -> None:
        processed = []

        @queue_processors.assign_queue('unreliable_worker')
        class UnreliableWorker(queue_processors.QueueProcessingWorker):
            def consume(self, data: Mapping[str, Any]) -> None:
                if data["type"] == 'unexpected behaviour':
                    raise Exception('Worker task not performing as expected!')
                processed.append(data["type"])

            def _log_problem(self) -> None:

                # keep the tests quiet
                pass

        fake_client = self.FakeClient()
        for msg in ['good', 'fine', 'unexpected behaviour', 'back to normal']:
            fake_client.queue.append(('unreliable_worker', {'type': msg}))

        fn = os.path.join(settings.QUEUE_ERROR_DIR, 'unreliable_worker.errors')
        try:
            os.remove(fn)
        except OSError:  # nocoverage # error handling for the directory not existing
            pass

        with simulated_queue_client(lambda: fake_client):
            worker = UnreliableWorker()
            worker.setup()
            worker.start()

        self.assertEqual(processed, ['good', 'fine', 'back to normal'])
        line = open(fn).readline().strip()
        event = ujson.loads(line.split('\t')[1])
        self.assertEqual(event["type"], 'unexpected behaviour')

    def test_worker_noname(self) -> None:
        class TestWorker(queue_processors.QueueProcessingWorker):
            def __init__(self) -> None:
                super().__init__()

            def consume(self, data: Mapping[str, Any]) -> None:
                pass  # nocoverage # this is intentionally not called
        with self.assertRaises(queue_processors.WorkerDeclarationException):
            TestWorker()

    def test_worker_noconsume(self) -> None:
        @queue_processors.assign_queue('test_worker')
        class TestWorker(queue_processors.QueueProcessingWorker):
            def __init__(self) -> None:
                super().__init__()

        with self.assertRaises(queue_processors.WorkerDeclarationException):
            worker = TestWorker()
            worker.consume({})

    def test_get_active_worker_queues(self) -> None:
        worker_queue_count = (len(QueueProcessingWorker.__subclasses__()) +
                              len(LoopQueueProcessingWorker.__subclasses__()) - 1)
        self.assertEqual(worker_queue_count, len(get_active_worker_queues()))
        self.assertEqual(1, len(get_active_worker_queues(queue_type='test')))
