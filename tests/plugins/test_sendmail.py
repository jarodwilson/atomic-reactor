import smtplib

from dockerfile_parse import DockerfileParser
from flexmock import flexmock
import pytest
import requests
import six

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.exit_sendmail import SendMailPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import ImageName


class TestSendMailPlugin(object):
    def test_fails_with_unknown_states(self):
        p = SendMailPlugin(None, None, send_on=['unknown_state', 'manual_fail'])
        with pytest.raises(PluginFailedException) as e:
            p.run()
        assert str(e.value) == 'Unknown state(s) "unknown_state" for sendmail plugin'

    @pytest.mark.parametrize('rebuild, success, canceled, send_on, expected', [
        # make sure that right combinations only succeed for the specific state
        (False, True, False, ['manual_success'], True),
        (False, True, False, ['manual_fail', 'auto_success', 'auto_fail', 'auto_canceled'],
         False),
        (False, False, False, ['manual_fail'], True),
        (False, False, False, ['manual_success', 'auto_success', 'auto_fail', 'auto_canceled'],
         False),
        (True, True, False, ['auto_success'], True),
        (True, True, False, ['manual_success', 'manual_fail', 'auto_fail', 'auto_canceled'],
         False),
        (True, False, False, ['auto_fail'], True),
        (True, False, False, ['manual_success', 'manual_fail', 'auto_success', 'auto_canceled'],
         False),
        (True, False, True, ['auto_canceled'], True),
        # auto_fail would also give us True in this case
        (True, False, True, ['manual_success', 'manual_fail', 'auto_success'],
         False),
        # also make sure that a random combination of more plugins works ok
        (True, False, False, ['auto_fail', 'manual_success'], True)
    ])
    def test_should_send(self, rebuild, success, canceled, send_on, expected):
        p = SendMailPlugin(None, None, send_on=send_on)
        assert p._should_send(rebuild, success, canceled) == expected

    def test_render_mail(self):
        # just test a random combination of the method inputs and hope it's ok for other
        #   combinations
        class WF(object):
            image = ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
        p = SendMailPlugin(None, WF(), url='https://something.com')
        subject, body = p._render_mail(True, False, False)
        assert subject == 'Image foo/bar:baz; Status failed; Submitted by <autorebuild>'
        assert body == '\n'.join([
            'Image: foo/bar:baz',
            'Status: failed',
            'Submitted by: <autorebuild>',
            'Logs: https://something.com/builds/blablabla/log'
        ])

    def test_get_pdc_token(self):
        pass  # TODO

    @pytest.mark.parametrize('df_labels, pdc_component_df_label, expected', [
        ({}, 'Foo', None),
        ({'Foo': 'Bar'}, 'Foo', 'Bar'),
    ])
    def test_get_component_label(self, df_labels, pdc_component_df_label, expected):
        class WF(object):
            class builder(object):
                df_path = '/foo/bar'
        p = SendMailPlugin(None, WF(), pdc_component_df_label=pdc_component_df_label)
        flexmock(DockerfileParser, labels=df_labels)
        if expected is None:
            with pytest.raises(PluginFailedException):
                p._get_component_label()
        else:
            assert p._get_component_label() == expected

    def test_get_receivers_list_raises_unless_GitSource(self):
        class WF(object):
            source = None
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')

        with pytest.raises(PluginFailedException) as e:
            p._get_receivers_list()
        assert str(e.value) == 'Source is not of type "GitSource", panic!'

    def test_get_receivers_list_request_exception(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')
        flexmock(requests).should_receive('get').and_raise(requests.RequestException('foo'))

        with pytest.raises(RuntimeError) as e:
            p._get_receivers_list()
        assert str(e.value) == 'foo'

    def test_get_receivers_list_wrong_status_code(self):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')

        class R(object):
            status_code = 404
            text = 'bazinga!'
        flexmock(requests).should_receive('get').and_return(R())

        with pytest.raises(RuntimeError) as e:
            p._get_receivers_list()
        assert str(e.value) == 'PDC returned non-200 status code (404), see referenced build log'

    @pytest.mark.parametrize('pdc_response, expected', [
        ({'results': []},
         'Expected to find exactly 1 PDC component, found 0, see referenced build log'),
        ({'results': [{'dist_git_branch': 'foo'}, {'dist_git_branch': 'foo'}]},
         'Expected to find exactly 1 PDC component, found 2, see referenced build log'),
        ({'results': [{'dist_git_branch': 'foo', 'contacts': []}]},
         'no Build_Owner role for the component'),
        ({'results': [{'dist_git_branch': 'foo',
                       'contacts': [{'contact_role': 'Build_Owner', 'email': 'foo@bar.com'}]}]},
         ['foo@bar.com']),
        ({'results': [{'dist_git_branch': 'foo',
                       'contacts':
                       [{'contact_role': 'Build_Owner', 'email': 'foo@bar.com'},
                        {'contact_role': 'Build_Owner', 'email': 'spam@spam.com'},
                        {'contact_role': 'different', 'email': 'other@baz.com'}]}]},
         ['foo@bar.com', 'spam@spam.com']),
    ])
    def test_get_receivers_pdc_actually_responds(self, pdc_response, expected):
        class WF(object):
            source = GitSource('git', 'foo', provider_params={'git_commit': 'foo'})
        p = SendMailPlugin(None, WF())
        flexmock(p).should_receive('_get_component_label').and_return('foo')

        class R(object):
            status_code = 200

            def json(self):
                return pdc_response
        flexmock(requests).should_receive('get').and_return(R())

        if isinstance(expected, str):
            with pytest.raises(RuntimeError) as e:
                p._get_receivers_list()
            assert str(e.value) == expected
        else:
            assert p._get_receivers_list() == expected

    def test_send_mail(self):
        p = SendMailPlugin(None, None, from_address='foo@bar.com', smtp_url='smtp.spam.com')

        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        flexmock(smtp_inst).should_receive('sendmail').\
            with_args('foo@bar.com', ['spam@spam.com'], str)
        flexmock(smtp_inst).should_receive('quit')
        p._send_mail(['spam@spam.com'], 'subject', 'body')

    def test_run_ok(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        receivers = ['foo@bar.com', 'x@y.com']
        p = SendMailPlugin(None, WF(), send_on=['auto_fail'])

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(True)
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_send_mail').with_args(receivers, six.text_type, six.text_type)

        p.run()

    def test_run_fails_to_obtain_receivers(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        error_addresses = ['error@address.com']
        p = SendMailPlugin(None, WF(), send_on=['auto_fail'], error_addresses=error_addresses)

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(True)
        flexmock(p).should_receive('_get_receivers_list').and_raise(RuntimeError())
        flexmock(p).should_receive('_send_mail').with_args(error_addresses, six.text_type,
                                                           six.text_type)

        p.run()

    def test_run_does_nothing_if_conditions_not_met(self):
        class WF(object):
            build_failed = True
            autorebuild_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = ImageName.parse('repo/name')
        p = SendMailPlugin(None, WF(), send_on=['manual_success'])

        flexmock(p).should_receive('_should_send').with_args(True, False, False).and_return(False)
        flexmock(p).should_receive('_get_receivers_list').times(0)
        flexmock(p).should_receive('_send_mail').times(0)

        p.run()
