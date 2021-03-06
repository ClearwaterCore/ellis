# @file homestead.py
#
# Project Clearwater - IMS in the Cloud
# Copyright (C) 2013  Metaswitch Networks Ltd
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version, along with the "Special Exception" for use of
# the program along with SSL, set forth below. This program is distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
# The author can be reached by email at clearwater@metaswitch.com or by
# post at Metaswitch Networks Ltd, 100 Church St, Enfield EN2 6BQ, UK
#
# Special Exception
# Metaswitch Networks Ltd  grants you permission to copy, modify,
# propagate, and distribute a work formed by combining OpenSSL with The
# Software, or a work derivative of such a combination, even if such
# copying, modification, propagation, or distribution would otherwise
# violate the terms of the GPL. You must comply with the GPL in all
# respects for all of the code used other than OpenSSL.
# "OpenSSL" means OpenSSL toolkit software distributed by the OpenSSL
# Project and licensed under the OpenSSL Licenses, or a work based on such
# software and licensed under the OpenSSL Licenses.
# "OpenSSL Licenses" means the OpenSSL License and Original SSLeay License
# under which the OpenSSL Project distributes the OpenSSL toolkit software,
# as those licenses appear in the file LICENSE-OPENSSL.


import logging
import urllib
import json
import re
import datetime
import time

from tornado import httpclient
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPError

from metaswitch.ellis import settings
from metaswitch.common import utils

from functools import partial

_log = logging.getLogger("ellis.remote")


def ping(callback=None):
    """Make sure we can reach homestead"""
    homestead_client = httpclient.AsyncHTTPClient()
    url = _ping_url()

    def default_callback(response):
        if response.body == "OK":
            _log.info("Pinged Homestead OK")
        else:
            # Shouldn't happen, as if we can reach the server, it should
            # respond with OK If it doesn't assume we've reached the wrong
            # machine
            _log.error("Failed to ping Homestead at %s."
                       " Have you configured your HOMESTEAD_URL?" % url)

    homestead_client.fetch(url, callback or default_callback)


def get_digest(private_id, callback):
    """
    Retreives a digest from Homestead for a given private id
    callback receives the HTTPResponse object. Note the homestead api returns
    {"digest_ha1": "<digest>", "realm": "<realm>"}, rather than just the digest
    """
    url = _private_id_url(private_id)
    _http_request(url, callback, method='GET')


def create_private_id(private_id, realm, password, callback, plaintext=False):
    """Creates a private ID and associates it with an implicit
    registration set."""
    password_resp = put_password(private_id, realm, password, None, plaintext=plaintext)
    # Having no callback makes this synchronous - but check for errors
    if isinstance(password_resp, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, password_resp))
        return None
    irs_url = _new_irs_url()
    irs_resp = _sync_http_request(irs_url, method="POST", body="")
    if isinstance(irs_resp, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, irs_resp))
        return None
    uuid = _get_irs_uuid(_location(irs_resp))
    url = _associate_new_irs_url(private_id, uuid)

    # We have to do this synchronously and then call the callback with
    # its response - otherwise we return before the IMPI and IRS are
    # associated and subsequent steps fail
    response = _sync_http_request(url, method="PUT", body="")
    IOLoop.instance().add_callback(partial(callback, response))


def put_password(private_id, realm, password, callback, plaintext=False):
    """
    Posts a new password to Homestead for a given private id
    callback receives the HTTPResponse object.
    """
    url = _private_id_url(private_id)
    if plaintext:
        body = json.dumps({"plaintext_password": password, "realm": realm})
    else:
        digest = utils.md5("%s:%s:%s" % (private_id,
                                         realm,
                                         password))
        body = json.dumps({"digest_ha1": digest, "realm": realm})
    headers = {"Content-Type": "application/json"}
    if callback:
        _http_request(url, callback, method='PUT', headers=headers, body=body)
    else:
        return _sync_http_request(url, method="PUT", headers=headers, body=body)


def delete_private_id(private_id, callback):
    """
    Deletes a password from Homestead for a given private id
    callback receives the HTTPResponse object.
    """
    irs_url = _associated_irs_url(private_id)
    associated_irs_response = _sync_http_request(irs_url, method='GET')
    if isinstance(associated_irs_response, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, associated_irs_response))
        return None
    irs = json.loads(associated_irs_response.body)['associated_implicit_registration_sets'][0]

    irs_deletion = _sync_http_request(_irs_url(irs), method='DELETE')
    if isinstance(irs_deletion, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, irs_deletion))
        return None
    url = _private_id_url(private_id)
    _http_request(url, callback, method='DELETE')


def get_associated_publics(private_id, callback):
    """
    Retrieves the associated public identities for a given private identity
    from Homestead. callback receives the HTTPResponse object.
    """
    url = _associated_public_url(private_id)
    _http_request(url, callback, method='GET')


def create_public_id(private_id, public_id, ifcs, callback):
    """
    Posts a new public identity to associate with a given private identity
    to Homestead. Also sets the given iFCs for that public ID.
    callback receives the HTTPResponse object.
    """
    url = _associated_irs_url(private_id)
    resp1 = _sync_http_request(url, method='GET')
    if isinstance(resp1, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, resp1))
        return None
    _log.info(resp1.body)
    irs = json.loads(resp1.body)['associated_implicit_registration_sets'][0]
    sp_url = _new_service_profile_url(irs)
    resp2 = _sync_http_request(sp_url, method='POST', body="")
    if isinstance(resp2, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, resp2))
        return None
    sp = _get_sp_uuid(_location(resp2))
    public_url = _new_public_id_url(irs, sp, public_id)
    body = "<PublicIdentity><Identity>" + \
           public_id + \
           "</Identity></PublicIdentity>"
    resp3 = _sync_http_request(public_url, method='PUT', body=body)
    if isinstance(resp3, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, resp3))
        return None
    put_filter_criteria(public_id, ifcs, callback)


def delete_public_id(public_id, callback):
    """
    Deletes an association between a public and private identity in Homestead
    callback receives the HTTPResponse object.
    """
    public_to_sp_url = _sp_from_public_id_url(public_id)
    response = _sync_http_request(public_to_sp_url, method='GET')
    if isinstance(response, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, response))
        return None
    service_profile = _location(response)
    url = _url_host() + _make_url_without_prefix(service_profile + "/public_ids/{}", public_id)
    resp2 = _sync_http_request(url, method='DELETE')
    if isinstance(resp2, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, resp2))
        return None
    _http_request(_url_host() + service_profile, callback, method='DELETE')


def get_associated_privates(public_id, callback):
    """
    Retrieves the associated private identities for a given public identity
    from Homestead.
    callback receives the HTTPResponse object.
    """
    url = _associated_private_url(public_id)
    _http_request(url, callback, method='GET')


def get_filter_criteria(public_id, callback):
    """
    Retrieves the filter criteria associated with the given public ID.
    """
    sp_url = _sp_from_public_id_url(public_id)
    sp_resp = _sync_http_request(sp_url, method='GET')
    if isinstance(sp_resp, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, sp_resp))
        return None
    sp_location = _location(sp_resp)
    url = _url_host() + _make_url_without_prefix(sp_location + "/filter_criteria")
    _http_request(url, callback, method='GET')


def put_filter_criteria(public_id, ifcs, callback):
    """
    Updates the initial filter criteria in Homestead for the given line.
    callback receives the HTTPResponse object.
    """
    sp_url = _sp_from_public_id_url(public_id)
    resp = _sync_http_request(sp_url, method='GET')
    if isinstance(resp, HTTPError): # pragma: no cover
        IOLoop.instance().add_callback(partial(callback, resp))
        return None
    sp_location = _location(resp)
    url = _url_host() + _make_url_without_prefix(sp_location + "/filter_criteria")
    _http_request(url, callback, method='PUT', body=ifcs)

def get_public_ids(chunk, chunk_proportion, excludeuuids, callback):
    """
    Retrieves all public IDs (possibly by chunk) provisioned on Homestead.
    """
    url = _public_ids_url(chunk, chunk_proportion, excludeuuids)
    _http_request(url, callback, method='GET')


# Utility functions

def _location(httpresponse):
    """Retrieves the Location header from this HTTP response,
    throwing a 500 error if it is missing"""
    if httpresponse.headers.get_list('Location'):
        return httpresponse.headers.get_list('Location')[0]
    else: # pragma: no cover
        _log.error("Could not retrieve Location header from HTTPResponse %s" % httpresponse)
        raise HTTPError(500)


def _http_request(url, callback, overload_retries=3, **kwargs):
    http_client = httpclient.AsyncHTTPClient()
    if 'follow_redirects' not in kwargs:
        kwargs['follow_redirects'] = False
    kwargs['allow_ipv6'] = True
    # Use a holder for the retries value, so that the functions we define below can modify it.
    retries_holder = {'retries': overload_retries}

    def do_http_request():
        _log.info("Sending HTTP %s request to %s",
                  kwargs.get('method', 'GET'),
                  url)
        http_client.fetch(url, callback_wrapper, **kwargs)

    def callback_wrapper(response):
        _log.debug("Received response from %s with code %d" % (url, response.code))
        if response.code == 503 and retries_holder['retries'] > 1:
            _log.debug("503 response - retrying %d more time(s)..." % retries_holder['retries'])
            retries_holder['retries'] -= 1
            # Set a timer to retry the HTTP request in 500ms.
            IOLoop.instance().add_timeout(datetime.timedelta(milliseconds=500), do_http_request)
        else:
            callback(response)

    do_http_request()


def _sync_http_request(url, overload_retries=3, **kwargs):
    _log.info("Sending HTTP %s request to %s",
              kwargs.get('method', 'GET'),
              url)
    http_client = httpclient.HTTPClient()
    if 'follow_redirects' not in kwargs:
        kwargs['follow_redirects'] = False
    kwargs['allow_ipv6'] = True
    while True:
        try:
            return http_client.fetch(url, **kwargs)
        except HTTPError as e:
            if e.code == 303:
                return e.response
            elif e.code == 503 and overload_retries > 1:
                overload_retries -= 1
                time.sleep(0.5)
            else:
                return e
        except Exception as e:
            _log.error("Received exception {}, treating as 500 HTTP error".format(e))
            return HTTPError(500)


def _url_host():
    url = "http://%s" % (settings.HOMESTEAD_URL)
    return url


def _url_prefix():
    return _url_host() + "/"


def _ping_url():
    """Returns the ping URL for checking that the server is responsive"""
    return _make_url("ping")

def _private_id_url(private_id):
    """Returns the URL for accessing/setting/creating this private ID's
    password"""
    return _make_url("private/{}", private_id)


def _associated_public_url(private_id):
    """Returns the URL for learning this private ID's associated public IDs'"""
    return _make_url("private/{}/associated_public_ids", private_id)


def _associated_private_url(public_id):
    """Returns the URL for learning this public ID's associated private IDs'"""
    return _make_url("public/{}/associated_private_ids", public_id)


def _new_public_id_url(irs, service_profile, public_id):
    """Returns the URL for creating a new public ID in this service profile"""
    return _make_url("irs/{}/service_profiles/{}/public_ids/{}",
                     irs,
                     service_profile,
                     public_id)


def _new_irs_url():
    """Returns the URL for creating a new implicit registration set"""
    return _make_url("irs/")


def _new_service_profile_url(irs):
    """Returns the URL for creating a new service profile in this IRS"""
    return _make_url("irs/{}/service_profiles", irs)


def _associated_irs_url(private_id):
    """Returns the URL for learning this private ID's associated implicit
    registration sets"""
    return _make_url("private/{}/associated_implicit_registration_sets",
                     private_id)


def _irs_url(irs_uuid):
    """Returns the URL for deleting this implicit registration set"""
    return _make_url("irs/{}", irs_uuid)


def _associate_new_irs_url(private_id, irs):
    """Returns the URL for associating this private ID and IRS"""
    return _make_url("private/{}/associated_implicit_registration_sets/{}",
                     private_id,
                     irs)


def _sp_from_public_id_url(public_id):
    """Returns the URL for learning this public ID's service profile"""
    return _make_url('public/{}/service_profile', public_id)


def _public_ids_url(chunk, chunk_proportion, excludeuuids):
    """Returns the URL for retrieving all public IDs"""
    return _make_url("public/?excludeuuids={}&chunk-proportion={}&chunk={}",
                     "true" if excludeuuids else "false",
                     str(chunk_proportion),
                     str(chunk))


def _make_url_without_prefix(format_str, *args):
    """Makes a URL by URL-escaping the args, and interpolating them into
    format_str"""
    formatted_args = [urllib.quote_plus(arg) for arg in args]
    return format_str.format(*formatted_args)


def _make_url(format_str, *args):
    """Makes a URL by URL-escaping the args, interpolating them into
    format_str, and adding a prefix"""
    return _url_prefix() + _make_url_without_prefix(format_str, *args)


def _get_irs_uuid(url):
    """Retrieves the UUID of an Implicit Registration Set from a URL"""
    re_str = "irs/([^/]+)"
    match = re.search(re_str, url)
    if not match: # pragma: no cover
        raise ValueError("URL %s is badly formatted: expected it to match %s" % (url, re_str))
    return match.group(1)


def _get_sp_uuid(url):
    """Retrieves the UUID of a Service Profile from a URL"""
    re_str = "irs/[^/]+/service_profiles/([^/]+)"
    match = re.search(re_str, url)
    if not match: # pragma: no cover
        raise ValueError("URL %s is badly formatted: expected it to match %s" % (url, re_str))
    return match.group(1)
