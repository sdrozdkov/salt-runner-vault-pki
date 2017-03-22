# -*- coding: utf-8 -*-
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4

"""Salt runner module to sign certificates with the Vault PKI backend.

Invoked by the vault_pki orchestrator which provides the necessary
keyword arguments to main(). (and follow-up steps after the runner exits)

At a glance this runner takes a minion fully-qualitified domain name
(FQDN), a certificate signing request (CSR), and a destination path on
the minion.

The CSR is verified, by way of checking the FQDN and the desired CN of
the certificate, and the configured validity period is set as configured
in the Salt master config file.

It then makes a request to a configured Vault instance using App-ID
authentication (yes this needs to change) and gets the CSR signed.

The resulting certificate, and a full chain (certificate appended with
the CA's certificate), are written back to the minion at the given
destination path.


Breakdown of the runner's steps:
    - verify CSR is valid, aka matches hostname, has
      expiration, etc.
    - open connection to vault and authenticate
    - send CSR to vault to be signed and retrieve cert
    - use version number to write cert and chain into
      proper place on minion

Steps that need to follow -- but the runner doesn't do:
    - run vault_pki activate $version_number
    - restart or otherwise inform servers a new certificate is in place
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

__author__ = 'Daniel Wilcox (dmw@ripple.com)'

import logging
import os
import six

import hvac

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from salt import client as salt_client
from salt import config as salt_config


CERT_FILENAME = 'cert.pem'
FULLCHAIN_FILENAME = 'fullchain.pem'

CERT_VALIDITY_PERIOD = '{:d}h'.format(30 * 24)

SALT_MASTER_CONFIG = '/etc/salt/master'

default_level = logging.INFO
log = logging.getLogger(__file__)
log.setLevel(default_level)
log_formatter = logging.Formatter(('%(asctime)s - %(name)s - %(levelname)s'
                                   ' - %(message)s'))
log_handler = logging.StreamHandler()
log_handler.setFormatter(log_formatter)
log_handler.setLevel(default_level)
log.addHandler(log_handler)


class ConfigError(Exception):
    """Error to raise if config is invalid or incomplete."""
    pass


class SigningError(Exception):
    """Error for issues with the CSR or signing operation."""
    pass


def get_user_id(source="~/.vault-id"):
    """ Reads a vault user-id (UUID) from a file."""
    source = os.path.abspath(os.path.expanduser(source))
    user_id = None

    # pylint: disable=invalid-name
    if os.path.isfile(source):
        fd = open(source, "r")
        user_id = fd.read().strip()
        fd.close()

    return user_id


def _verify_csr_ok(fqdn, csr_pem_data):
    """Confirms CSR contains only the FQDN of requesting minion.

    Makes assumption that CN will contain hostname despite SAN
    becoming much more common.  That plus the ability to verify
    some outside source for potential extra SANs would be helpful.
    """
    # TODO(dmw) Needs more thorough logging.
    csr_ok = False
    csr = x509.load_pem_x509_csr(csr_pem_data, default_backend())
    # TODO(dmw) Check subject alternative name (SAN) is valid as well.
    name_oid = x509.oid.NameOID.COMMON_NAME
    names = csr.subject.get_attributes_for_oid(name_oid)
    log.info('CSR has names {} for minion {}'.format(names, fqdn))
    log.info('CSR ({}): "{}"'.format(fqdn, csr_pem_data))
    if len(names) == 1:
        common_name = names[0].value
        if six.u(fqdn) == common_name:
            csr_ok = True
    return csr_ok


def _get_vault_connection(config):
    """Opens a connection to vault and returns it.

    Uses configuration from the salt master config file for the vault
    URL, app-id and user-id file.
    """
    try:
        conn = hvac.Client(url=config.get('url'))
        user_id_file = config.get('vault_user_id_file')
        if user_id_file:
            user_id = get_user_id(source=user_id_file)
        else:
            user_id = get_user_id()
        # TODO(dmw) Move to App Role authentication.
        conn.auth_app_id(config.get('app_id'), user_id)
    except hvac.exceptions.VaultError as err:
        log.error('Vault error: {}'.format(err))
        return None
    return conn


def _write_certs_to_minion(fqdn, dest_path, cert_data):
    """Writes signed cert back to requesting minion at specified path.

    Given a destination path on the minion, write both the signed cert
    and a full chain (cert + CA cert) to it using the standard filenames.
    """
    client = salt_client.LocalClient(SALT_MASTER_CONFIG)
    cert_path = os.path.join(dest_path, CERT_FILENAME)
    fullchain_path = os.path.join(dest_path, FULLCHAIN_FILENAME)
    cert = cert_data['certificate']
    fullchain = '\n'.join([cert, cert_data['issuing_ca']])
    write_cert = client.cmd(
        fqdn,
        'file.write',
        [cert_path, cert]
    )
    write_fullchain = client.cmd(
        fqdn,
        'file.write',
        [fullchain_path, fullchain]
    )
    # TODO(dmw) Figure out odd client.cmd rc's and error if needed.
    return True


def main(**kwargs):
    """Ferries CSR to Vault to be signed and writes back returned cert.

    Recieves keyword arguments from invocation by the vault_pki
    orchestrator.  Must include:
        host: string FQDN of the requesting minion
        csr: string PEM encoded certificate signing request (CSR)
        path: string destination path on the minion to write back certs
    """
    fqdn = kwargs.get('host')
    csr = kwargs.get('csr')
    dest_cert_path = kwargs.get('path')

    log.info('Received CSR for {}'.format(fqdn))
    full_config = salt_config.api_config(SALT_MASTER_CONFIG)
    config = full_config.get('vault_pki_runner')
    if _verify_csr_ok(fqdn, csr):
        vault_conn = _get_vault_connection(config)
        validity_period = config.get('validitiy_period',
                                     CERT_VALIDITY_PERIOD)
        signing_params = {'alt_names': six.u(fqdn),
                          'csr': csr,
                          'common_name': six.u(fqdn),
                          'format': 'pem',
                          'ttl': validity_period}
        pki_path = config.get('pki_path')
        if not pki_path:
            raise ConfigError('Missing required parameter "pki_path"')
        try:
            vault_response = vault_conn._post(pki_path, json=signing_params)
        except hvac.exceptions.VaultError as err:
            log.error('Vault error: {}'.format(err))
            raise SigningError('Error signing from vault!')
        cert_data = vault_response.json()['data']
        write_ok = _write_certs_to_minion(fqdn, dest_cert_path, cert_data)
        if not write_ok:
            log.error('Error writing cert to minion!')
        else:
            log.info('Wrote new certificate to {}'.format(fqdn))
    else:
        raise SigningError('CSR missing or invalid, check fqdn.')