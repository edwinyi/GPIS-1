# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Google Compute Engine helper class.

Use this class to:
- Start an instance
- List instances
- Delete an instance
"""

__author__ = 'kbrisbin@google.com (Kathryn Hurley)'

import logging
try:
  import simplejson as json
except:
  import json
import IPython
import time
import traceback

import multiprocessing as mp

from apiclient.discovery import build
from apiclient.errors import HttpError
import httplib2
from httplib2 import HttpLib2Error
from oauth2client.client import AccessTokenRefreshError
from oauth2client.client import flow_from_clientsecrets
from oauth2client.file import Storage
from oauth2client.tools import argparser, run_flow
from oauth2client import tools

class Disk(object):
  def __init__(self, name, mode='READ_ONLY'):
    self.name = name
    self.mode = mode

class Gce(object):
  """Demonstrates some of the image and instance API functionality.

  Attributes:
    settings: A dictionary of application settings from the settings.json file.
    service: An apiclient.discovery.Resource object for Compute Engine.
    project_id: The string Compute Engine project ID.
    project_url: The string URL of the Compute Engine project.
  """

  def __init__(self, auth_http, config, project_id=None):
    """Initialize the Gce object.

    Args:
      config: a yaml config file
      project_id: the API console project name
    """
    self.config = config
    self.auth_http = auth_http

    self.service = build(
        'compute', self.config['compute']['api_version'], http=self.auth_http)

    self.gce_url = 'https://www.googleapis.com/compute/%s/projects' % (
        self.config['compute']['api_version'])

    self.project_id = None
    if not project_id:
      self.project_id = self.config['project']
    else:
      self.project_id = project_id
    self.project_url = '%s/%s' % (self.gce_url, self.project_id)

  def start_instance(self,
                     instance_name,
                     disk_name,
                     image_name,
                     zone=None,
                     machine_type=None,
                     network=None,
                     service_email=None,
                     scopes=None,
                     metadata=None,
                     startup_script=None,
                     startup_script_url=None,
                     blocking=True,
                     additional_disks=[]):
    """Start an instance with the given name and settings.

    Args:
      instance_name: String name for instance.
      disk_name: The string disk name.
      image_name: The string image name.
      zone: The string zone name.
      machine_type: The string machine type.
      network: The string network.
      service_email: The string service email.
      scopes: List of string scopes.
      metadata: List of metadata dictionaries.
      startup_script: The filename of a startup script.
      startup_script_url: Url of a startup script.
      blocking: Whether the function will wait for the operation to complete.
      additional_disks: List of disk names

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      DiskDoesNotExistError: Disk to be used for instance boot does not exist.
      ValueError: Either instance_name is None an empty string or disk_name
          is None or an empty string.
    """
    if not instance_name:
      raise ValueError('instance_name required.')

    if not disk_name:
      raise ValueError('disk_name required.')

    if not image_name:
      raise ValueError('image_name required.')

    # Instance dictionary is sent in the body of the API request.
    instance = {}

    # Set required instance fields with defaults if not provided.
    instance['name'] = instance_name
    if not zone:
      zone = self.config['compute']['zones'][0]
    if not machine_type:
      machine_type = self.config['compute']['machine_type']
    instance['machineType'] = '%s/zones/%s/machineTypes/%s' % (
        self.project_url, zone, machine_type)
    if not network:
      network = self.config['compute']['network']
    instance['networkInterfaces'] = [{
        'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}],
        'network': '%s/global/networks/%s' % (self.project_url, network)}]

    # Make sure the disk exists, and apply disk to instance resource.
    image_url = '%s/global/images/%s' % (self.project_url, image_name)
    instance['disks'] = [{
      'boot': True,
      'type': self.config['disk_type'],
      'initializeParams': {
        'diskName': disk_name,
        'sourceImage': image_url
      }
    }]

    # Attach additional disks
    for disk in additional_disks:
      disk_valid = self.get_disk(disk.name, zone)
      if not disk_valid:
        logging.error('Disk %s does not exist.' % disk.name)
        raise DiskDoesNotExistError(disk.name)
      disk_info = {
        'boot': False,
        'type': self.config['disk_type'],
        'mode': disk.mode,
        'source': disk_valid['selfLink'],
      }
      instance['disks'].append(disk_info)

    # Set optional fields with provided values.
    if service_email or scopes:
      instance['serviceAccounts'] = [{'email': service_email, 'scopes': scopes}]

    # Set the instance metadata if provided.
    instance['metadata'] = {}
    instance['metadata']['items'] = []
    if metadata:
      instance['metadata']['items'].extend(metadata)

    # Set the instance startup script if provided.
    if startup_script:
      startup_script_resource = {
          'key': 'startup-script', 'value': open(startup_script, 'r').read()}
      instance['metadata']['items'].append(startup_script_resource)

    # Set the instance startup script URL if provided.
    if startup_script_url:
      startup_script_url_resource = {
          'key': 'startup-script-url', 'value': startup_script_url}
      instance['metadata']['items'].append(startup_script_url_resource)

    # Send the request.
    request = self.service.instances().insert(
        project=self.project_id, zone=zone, body=instance)
    response = self._execute_request(request)
    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])

    return response

  def attach_disk(self, instance_name, disk_name,
                  zone=None, disk_mode='READ_ONLY'):
    """Attaches a persistent disk to an instance.

    Args:
      instance_name: The string instance name
      disk_name: The string disk name

    Returns:
      Dictionary response representing the operation

    Raises:
      ApiOperationError: Operation contains an error message.
      DiskDoesNotExistError: Disk to be used for instance boot does not exist.
      ValueError: Either instance_name is None or an empty string or disk_name
          is None or an empty string.
    """
    if not instance_name:
      raise ValueError('instance_name required.')
    if not disk_name:
      raise ValueError('disk_name required.')
    if not zone:
      zone = self.config['compute']['zones'][0]

    # check if disk name is valid
    if not self.get_disk(disk_name, zone):
      raise DiskDoesNotExistError(disk_name)

    body = {
      'type': 'persistent',
      'mode': disk_mode,
      'source': disk_response['selfLink']
    }
    request = self.service.instances().attachDisk(
      project=self.project_id, zone=zone, instance=instance_name, body=body
    )
    response = self._execute_request(request)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])
    return response

  def list_instances(self, zone=None, list_filter=None):
    """Lists project instances.

    Args:
      zone: The string zone name.
      list_filter: String filter for list query.

    Returns:
      List of instances matching given filter.
    """

    if not zone:
      zone = self.config['compute']['zones'][0]

    request = None
    if list_filter:
      request = self.service.instances().list(
          project=self.project_id, zone=zone, filter=list_filter)
    else:
      request = self.service.instances().list(
          project=self.project_id, zone=zone)
    response = self._execute_request(request)

    if response and 'items' in response:
      return response['items']
    return []

  def stop_instance(self,
                    instance_name,
                    zone=None,
                    blocking=True):
    """Stops an instance.

    Args:
      instance_name: String name for the instance.
      zone: The string zone name.
      blocking: Whether the function will wait for the operation to complete.

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      ValueError: instance_name is None or an empty string.
    """
    if not instance_name:
      raise ValueError('instance_name required.')

    if not zone:
      zone = self.config['compute']['zones'][0]

    # Delete the instance.
    request = self.service.instances().delete(
        project=self.project_id, zone=zone, instance=instance_name)
    response = self._execute_request(request)
    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])

    return response

  def create_disk(self,
                  disk_name,
                  image_project=None,
                  image=None,
                  zone=None,
                  size_gb=500,
                  source_snapshot=None,
                  blocking=True):
    """Creates a new persistent disk.

    Args:
      disk_name: String name for the disk.
      image_project: The string name for the project of the image.
      image: String name of the image to apply to the disk.
      zone: The string zone name.
      size_gb: Int size of disk in GB
      source_snapshot: String id of the snapshot to source from
      blocking: Whether the function will wait for the operation to complete.

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      ValueError: disk_name is None or an empty string.
    """

    if not disk_name:
      raise ValueError('disk_name required.')

    # Disk dictionary is sent in the body of the API request.
    disk = {}

    # Set required disk fields with defaults if not provided.
    disk['name'] = disk_name
    disk['sizeGb'] = size_gb
    if source_snapshot:
      disk['sourceSnapshot'] = 'global/snapshots/%s' %(source_snapshot)
    if not zone:
      zone = self.config['compute']['zones'][0]
    source_image = None
    if image_project and image:
      source_image = '%s/%s/global/images/%s' % (
        self.gce_url, image_project, image)

    request = self.service.disks().insert(
        project=self.project_id,
        zone=zone,
        sourceImage=source_image,
        body=disk)
    response = self._execute_request(request)
    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])

    return response

  def snapshot_disk(self,
                    disk_name,
                    project,
                    zone,
                    blocking=True):
    """Creates a new persistent disk.

    Args:
      disk_name: String name for the disk.
      project: String name for the project
      zone: The string zone name.
      blocking: Whether the function will wait for the operation to complete.

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      ValueError: disk_name is None or an empty string.
    """

    if not disk_name:
      raise ValueError('disk_name required.')

    body = {}
    body['sourceDisk'] = disk_name
    body['name'] = disk_name + '-snapshot'

    request = self.service.disks().createSnapshot(
      disk=disk_name, project=project, zone=zone, body=body
      )
    response = self._execute_request(request)

    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])

    response['snapshot_name'] = body['name']
    return response

  def delete_snapshot(self,
                      snapshot_name,
                      project,
                      blocking=True):
    """Creates a new persistent disk.

    Args:
      snapshot_name: String name for the disk.
      project: String name for the project

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      ValueError: disk_name is None or an empty string.
    """

    if not snapshot_name:
      raise ValueError('snapshot_name required.')

    request = self.service.snapshots().delete(
      snapshot=snapshot_name, project=project
      )
    response = self._execute_request(request)

    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])
    return response

  def get_disk(self, disk_name, zone=None):
    """Gets the specified disk by name.

    Args:
      disk_name: The string name of the disk.
      zone: The string name of the zone.

    Returns:
      Dictionary response representing the disk or None if the disk
      does not exist.
    """

    if not zone:
      zone = self.config['compute']['zones'][0]

    request = self.service.disks().get(
        project=self.project_id, zone=zone, disk=disk_name)
    try:
      response = self._execute_request(request)
      return response
    except ApiError, e:
      return

  def delete_disk(self, disk_name, zone=None, blocking=True):
    """Deletes a disk.

    Args:
      disk_name: String name for the disk.
      zone: The string zone name.
      blocking: Whether the function will wait for the operation to complete.

    Returns:
      Dictionary response representing the operation.

    Raises:
      ApiOperationError: Operation contains an error message.
      ValueError: disk_name is None or an empty string.
    """

    if not disk_name:
      raise ValueError('disk_name required.')

    if not zone:
      zone = self.config['compute']['zones'][0]

    # Delete the disk.
    request = self.service.disks().delete(
        project=self.project_id, zone=zone, disk=disk_name)
    response = self._execute_request(request)
    if response and blocking:
      response = self._blocking_call(response)

    if response and 'error' in response:
      raise ApiOperationError(response['error']['errors'])

    return response

  def download_from_bucket(self, bucket, file_name, output_dir):
    """
    Downloads a file from the specified bucket under the GCE project.

    Args:
       bucket: String name for the bucket
       file_name: String name of file to download
       output_dir: String name of directory to download to

    Returns:
       True or false depending on success of download
    """
    


  def _blocking_call(self, response, finished_status='DONE'):
    """Blocks until the operation status is done for the given operation.

    Args:
      response: The response from the API call.

    Returns:
      Dictionary response representing the operation.
    """

    status = response['status']

    while status != finished_status and response:
      operation_id = response['name']
      if 'zone' in response:
        zone = response['zone'].rsplit('/', 1)[-1]
        request = self.service.zoneOperations().get(
            project=self.project_id, zone=zone, operation=operation_id)
      else:
        request = self.service.globalOperations().get(
            project=self.project_id, operation=operation_id)
      response = self._execute_request(request)
      if response:
        status = response['status']
        logging.info(
          'Waiting until operation is %s. Current status: %s',
          finished_status, status)
        if status != finished_status:
          time.sleep(3)

    return response

  def _execute_request(self, request):
    """Helper method to execute API requests.

    Args:
      request: The API request to execute.

    Returns:
      Dictionary response representing the operation if successful.

    Raises:
      ApiError: Error occurred during API call.
    """
    try:
      response = request.execute()
    except AccessTokenRefreshError, e:
      logging.error('Access token is invalid.')
      raise ApiError(e)
    except HttpError, e:
      logging.error('Http response was not 2xx.')
      logging.error(str(e))
      raise ApiError(e)
    except HttpLib2Error, e:
      logging.error('Transport error.')
      raise ApiError(e)
    except Exception, e:
      logging.error('Unexpected error occured.')
      traceback.print_stack()
      raise ApiError(e)
    return response

class Error(Exception):
  """Base class for exceptions in this module."""
  pass


class ApiError(Error):
  """Error occurred during API call."""
  pass


class ApiOperationError(Error):
  """Raised when an API operation contains an error."""

  def __init__(self, error_list):
    """Initialize the Error.

    Args:
      error_list: the list of errors from the operation.
    """

    super(ApiOperationError, self).__init__()
    self.error_list = error_list

  def __str__(self):
    """String representation of the error."""

    return repr(self.error_list)


class DiskDoesNotExistError(Error):
  """Disk to be used for instance boot does not exist."""
  pass
