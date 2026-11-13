"""Readiness must follow an actual HTTP response, not merely process creation."""
from urllib.error import URLError
from unittest.mock import Mock

import pytest
import run as launcher


def test_waits_through_connection_refusal_until_http_ready(monkeypatch):
    response=Mock(status=200)
    response.__enter__=Mock(return_value=response)
    response.__exit__=Mock(return_value=False)
    request=Mock(side_effect=[URLError('connection refused'),response])
    sleep=Mock()
    monkeypatch.setattr(launcher,'urlopen',request)
    monkeypatch.setattr(launcher.time,'sleep',sleep)
    launcher.wait_for_web([('Flask',Mock(poll=Mock(return_value=None)))],5000)
    assert request.call_count==2 and sleep.call_count==1


def test_reports_child_failure_instead_of_claiming_ready(monkeypatch):
    request=Mock()
    monkeypatch.setattr(launcher,'urlopen',request)
    with pytest.raises(RuntimeError,match='Flask stopped.*exit code 1'):
        launcher.wait_for_web([('Flask',Mock(poll=Mock(return_value=1)))],5000)
    request.assert_not_called()


def test_reports_readiness_timeout(monkeypatch):
    monkeypatch.setattr(launcher,'urlopen',Mock(side_effect=URLError('connection refused')))
    monkeypatch.setattr(launcher.time,'monotonic',Mock(side_effect=[0,91]))
    with pytest.raises(RuntimeError,match='did not become ready'):
        launcher.wait_for_web([('Flask',Mock(poll=Mock(return_value=None)))],5000)
