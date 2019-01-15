# *****************************************************************************
# © Copyright IBM Corp. 2018.  All Rights Reserved.
#
# This program and the accompanying materials
# are made available under the terms of the Apache V2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
# *****************************************************************************

import os
import tempfile
import dill as pickle
import requests
import datetime
from urllib.parse import quote, urlparse
from base64 import b64encode
import hashlib
import hmac
from lxml import etree
import logging
logger = logging.getLogger(__name__)
try:
    import ibm_boto3
    from ibm_boto3.s3.transfer import S3Transfer
    from ibm_botocore.client import Config
except (ImportError,ModuleNotFoundError):
    IBMBOTO_INSTALLED = False
    msg = 'ibm_boto3 is not installed. Use HMAC credentials to communicate with COS.'
    logger.info(msg)
else:
    IBMBOTO_INSTALLED = True

class CosClient:
    '''
    Cloud Object Storage client
    '''
    def __init__(self, credentials):
        self._cod_hmac_access_key_id = credentials['objectStorage']['username']
        self._cod_hmac_secret_access_key = credentials['objectStorage']['password']
        self._cos_region = credentials['objectStorage']['region']
        self._cos_endpoint = credentials['config']['objectStorageEndpoint']

        if self._cos_region is None or len(self._cos_region.strip()) == 0:
            self._cos_region = 'any-region'

    # hashing and signing methods
    def _hash(self, key, msg):
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

    # region is a wildcard value that takes the place of the AWS region value
    # as COS doen't use the same conventions for regions, this parameter can accept any string
    def _create_signature_key(self, key, datestamp, region, service):
        keyDate = self._hash(('AWS4' + key).encode('utf-8'), datestamp)
        keyString = self._hash(keyDate, region)
        keyService = self._hash(keyString, service)
        keySigning = self._hash(keyService, 'aws4_request')
        return keySigning

    def _cos_api_request(self, http_method, bucket, key, request_parameters=None, payload='', extra_headers=None, binary=False):
        if extra_headers is None:
            extra_headers = {}
        # it seems region is not used by IBM COS and can be any string (but cannot be None below still)
        if any([(var is None or len(var.strip()) == 0) for var in [self._cod_hmac_access_key_id, self._cod_hmac_secret_access_key, self._cos_endpoint, bucket]]):
            logger.warning('write COS is disabled because not all COS config environment variables are set')
            return None
        # assemble the standardized request

        time = datetime.datetime.utcnow()
        timestamp = time.strftime('%Y%m%dT%H%M%SZ')
        datestamp = time.strftime('%Y%m%d')

        url = urlparse(self._cos_endpoint)
        scheme = url.scheme
        host = url.netloc

        payload_hash = hashlib.sha256(str.encode(payload) if isinstance(payload, str) else payload).hexdigest()
        standardized_resource = '/'
        if bucket is not None:
            standardized_resource += bucket
        if key is not None:
            standardized_resource += '/' + key
        if request_parameters is None:
            standardized_querystring = ''
        else:
            standardized_querystring = '&'.join(['%s=%s' % (quote(k, safe=''), quote(v, safe='')) for k,v in request_parameters.items()])

        all_headers = {'host': host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': timestamp}
        all_headers.update({k.lower(): v for k, v in extra_headers.items()})
        standardized_headers = ''
        for header in sorted(all_headers.keys()):
            standardized_headers += '%s:%s\n' % (header, all_headers[header])
        signed_headers = ';'.join(sorted(all_headers.keys()))
        # standardized_headers = 'host:' + host + '\n' + 'x-amz-content-sha256:' + payload_hash + '\n' + 'x-amz-date:' + timestamp + '\n'
        # signed_headers = 'host;x-amz-content-sha256;x-amz-date'

        standardized_request = (http_method + '\n' +
                                standardized_resource + '\n' +
                                standardized_querystring + '\n' +
                                standardized_headers + '\n' +
                                signed_headers + '\n' +
                                payload_hash)

        logging.debug('standardized_request=\n%s' % standardized_request)

        # assemble string-to-sign
        hashing_algorithm = 'AWS4-HMAC-SHA256'
        credential_scope = datestamp + '/' + self._cos_region + '/' + 's3' + '/' + 'aws4_request'
        sts = (hashing_algorithm + '\n' +
               timestamp + '\n' +
               credential_scope + '\n' +
               hashlib.sha256(str.encode(standardized_request)).hexdigest())

        logging.debug('string-to-sign=\n%s' % sts)

        # generate the signature
        signature_key = self._create_signature_key(self._cod_hmac_secret_access_key, datestamp, self._cos_region, 's3')
        signature = hmac.new(signature_key,
                             (sts).encode('utf-8'),
                             hashlib.sha256).hexdigest()

        logging.debug('signature=\n%s' % signature)

        # assemble all elements into the 'authorization' header
        v4auth_header = (hashing_algorithm + ' ' +
                         'Credential=' + self._cod_hmac_access_key_id + '/' + credential_scope + ', ' +
                         'SignedHeaders=' + signed_headers + ', ' +
                         'Signature=' + signature)

        logging.debug('v4auth_header=\n%s' % v4auth_header)

        # the 'requests' package autmatically adds the required 'host' header
        headers = all_headers.copy()
        headers.pop('host')
        headers['Authorization'] = v4auth_header
        # headers = {'x-amz-content-sha256': payload_hash, 'x-amz-date': timestamp, 'Authorization': v4auth_header}

        if standardized_querystring == '':
            request_url = self._cos_endpoint + standardized_resource
        else:
            request_url = self._cos_endpoint + standardized_resource + '?' + standardized_querystring

        logging.debug('request_url=%s' % request_url)

        if http_method == 'GET':
            resp = requests.get(request_url, headers=headers, timeout=30)
        elif http_method == 'DELETE':
            resp = requests.delete(request_url, headers=headers, timeout=30)
        elif http_method == 'POST':
            resp = requests.post(request_url, headers=headers, data=payload, timeout=30)
        elif http_method == 'PUT':
            resp = requests.put(request_url, headers=headers, data=payload, timeout=30)
        else:
            raise RuntimeError('unsupported_http_method=%s' % http_method)

        if resp.status_code != requests.codes.ok and not (resp.status_code == requests.codes.no_content and http_method == 'DELETE'):
            logger.warning('error cos_api_request: request_url=%s, http_method=%s, status_code=%s, response_text=%s' % (request_url, http_method, str(resp.status_code), str(resp.text)))
            return None

        return resp.content if binary else resp.text

    def cos_get(self, key, bucket, request_parameters=None, binary=False):

        response = self._cos_api_request('GET', bucket=bucket, key=key, request_parameters=request_parameters, binary=binary)
        if response is not None:
            response = pickle.loads(response)

        return response

    def cos_find(self, prefix, bucket):
        result = self.cos_get(key=None, bucket=bucket, request_parameters={'list-type':'2','prefix':prefix})
        if result is None:
            return []

        root = etree.fromstring(str.encode(result))
        return [elem.text for elem in root.findall('Contents/Key', root.nsmap)]

    def cos_put(self, key, payload, bucket, binary=False):
        if payload is not None:
            payload = pickle.dumps(payload)
        else:
            payload = ''

        return self._cos_api_request('PUT', bucket=bucket, key=key, payload=payload, binary=binary)


    def cos_delete(self, key, bucket):
        return self._cos_api_request('DELETE', bucket=bucket, key=key)


    def cos_delete_multiple(self, keys, bucket):
        if keys is None or len(keys) == 0:
            return ''

        payload = '<?xml version="1.0" encoding="UTF-8"?><Delete>'
        for key in keys:
            payload += '<Object><Key>%s</Key></Object>' % key
        payload += '</Delete>'

        md5 = hashlib.md5(str.encode(payload)).digest()
        base64 = b64encode(md5).decode()
        logger.debug('content-md5: %s' % base64)

        extra_headers = {
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-MD5': base64
        }

        request_parameters = {'delete': ''}
        return self._cos_api_request('POST', bucket=bucket, key=None, payload=payload, request_parameters=request_parameters, extra_headers=extra_headers)
    

def cosSave(obj,bucket,filename,credentials):
    '''
    Use IAM credentials to write an object to Cloud Object Storage
    '''
    try:
        fhandle, fname = tempfile.mkstemp("cosfile")
        os.close(fhandle) 
        with open(fname, 'wb') as file_obj:
            pickle.dump(obj, file_obj)
        transfer = getCosTransferAgent(credentials)
        transfer.upload_file(fname, bucket, filename)
        os.unlink(fname)

    except Exception as ex:
        logging.exception(ex)
    return filename



def cosLoad(bucket,filename,credentials):
    '''
    Use IAM credentials to read an object from Cloud Object Storage
    '''
    try:
        fhandle, fname = tempfile.mkstemp("cosfile")
        os.close(fhandle)
        transfer = getCosTransferAgent(credentials)
        transfer.download_file(bucket, filename, fname)
        answer = None
        with open(fname, 'rb') as file_obj:
            answer = pickle.load(file_obj)
        os.unlink(fname)
        return answer

    except Exception as ex:
        logging.exception(ex)



def getCosTransferAgent(credentials):
    '''
    Use IAM credentials to obtain a Cloud Object Storage transfer agent object
    '''
    if IBMBOTO_INSTALLED:
        endpoints = requests.get(credentials.get('endpoints')).json()
        iam_host = (endpoints['identity-endpoints']['iam-token'])
        cos_host = (endpoints['service-endpoints']['cross-region']['us']['public']['us-geo'])
        api_key = credentials.get('apikey')
        service_instance_id = credentials.get('resource_instance_id')
        auth_endpoint = "https://" + iam_host + "/oidc/token"
        service_endpoint = "https://" + cos_host
        cos = ibm_boto3.client('s3',
                               ibm_api_key_id=api_key,
                               ibm_service_instance_id=service_instance_id,
                               ibm_auth_endpoint=auth_endpoint,
                               config=Config(signature_version='oauth'),
                               endpoint_url=service_endpoint)
        return S3Transfer(cos)
    else:
        raise ValueError('Attempting to use IAM credentials to communicate with COS. IBMBOTO is not installed. You make use HMAC credentials and the CosClient instead.')

def log_df_info(df,msg,include_data=False):
    '''
    Log a debugging entry showing first row and index structure
    '''
    try:
        msg = msg + ' | df count: %s ' %(len(df.index))
        if df.index.names != [None]:
            msg = msg + ' | df index: %s ' %(','.join(df.index.names))
        else:
            msg = msg + ' | df index is unnamed'
        if include_data:
            msg = msg + ' | 1st row: '
            try:
                cols = df.head(1).squeeze().to_dict()    
                for key,value in list(cols.items()):
                    msg = msg + '%s : %s, ' %(key, value)
            except AttributeError:
                msg = msg + str(df.head(1))
        else:
            msg = msg + ' | columns: %s' %(','.join(list(df.columns)))
        logger.debug(msg)
        return msg
    except Exception:
        logger.warn('dataframe contents not logged due to an unknown logging error')
        return ''