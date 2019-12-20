# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The whl modules defines classes for interacting with Python packages."""

import argparse
import os
import pkg_resources
import pkginfo
import re
import zipfile
from email.parser import Parser


class WheelMetadata(pkg_resources.FileMetadata):
  """Metadata handler for Wheels

  This provider acts like FileMetadata, but returns
  """

  def __init__(self, path):
    self.path = path

  def get_metadata(self, name):
    if name != 'METADATA':
      raise KeyError("No metadata except METADATA is available")

    basename = os.path.basename(self.path)
    parts = basename.split('-')
    distribution, version = parts[0], parts[1]
    metadata_path = '{}-{}.dist-info/METADATA'.format(distribution, version)

    with zipfile.ZipFile(self.path) as zf:
      metadata = zf.read(metadata_path).decode('ascii', 'ignore')
    return metadata


class Wheel(object):

  def __init__(self, path):
    self._path = path

  @property
  def _dist(self):
    try:
      return self.__dist
    except AttributeError:
      metadata = WheelMetadata(self.path())
      self.__dist = pkg_resources.DistInfoDistribution.from_filename(self.path(), metadata)
      return self.__dist

  def path(self):
    return self._path

  def basename(self):
    return os.path.basename(self.path())

  def distribution(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[0]

  def version(self):
    # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
    parts = self.basename().split('-')
    return parts[1]

  def repository_suffix(self):
    # Returns a canonical suffix that will form part of the name of the Bazel
    # repository for this package.
    canonical = 'pypi__{}_{}'.format(self.distribution(), self.version())
    # Escape any illegal characters with underscore.
    return re.sub('[-.+]', '_', canonical)

  def _dist_info(self):
    # Return the name of the dist-info directory within the .whl file.
    # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
    #      google_cloud-0.27.0.dist-info
    return '{}-{}.dist-info'.format(self.distribution(), self.version())

  def metadata(self):
    # Extract the structured data from METADATA
    return self._parse_with_pkginfo(pkginfo.Wheel(self.path()))

  def name(self):
    return self.metadata().get('name')

  def dependencies(self, extra=None):
    """Access the dependencies of this Wheel.

    Args:
      extra: if specified, include the additional dependencies
            of the named "extra".

    Yields:
      the names of requirements from the metadata.json
    """
    requires = set(self._dist.requires())
    if extra:
      requires = set(self._dist.requires(extras=(extra,))) - requires

    dependency_set = set()
    for r in requires:
      name = r.project_name
      if r.extras:
        name += "[{0}]".format(",".join(sorted(r.extras)))
      dependency_set.add(name)
    return dependency_set

  def extras(self):
    return self.metadata().get('extras', [])

  def expand(self, directory):
    with zipfile.ZipFile(self.path(), 'r') as whl:
      whl.extractall(directory)

  # _parse_with_pkginfo parses metadata from pkginfo.Wheel object.
  def _parse_with_pkginfo(self, pkg):
    metadata = { 'name' : pkg.name }
    if len(pkg.provides_extras) > 0:
      metadata['extras'] = sorted(set(pkg.provides_extras))
    metadata['run_requires'] = []
    for requirement_str in pkg.requires_dist:
      requirement = pkg_resources.Requirement.parse(requirement_str)

      r = {}
      r['requires'] = [requirement.name]
      r['requires'].extend([requirement.name + "[{0}]".format(extra)
                            for extra in requirement.extras])

      if requirement.marker:
        r['environment'] = str(requirement.marker)

        EXTRA_RE = re.compile("extra == ['\"](.*)['\"]")
        extra_match = EXTRA_RE.search(str(requirement.marker))
        if extra_match:
          r['extra'] = extra_match.group(1)

      metadata['run_requires'].append(r)

    return metadata

  # _parse_metadata parses METADATA files according to https://www.python.org/dev/peps/pep-0314/
  def _parse_metadata(self, content):
    # TODO: handle fields other than just name
    name_pattern = re.compile('Name: (.*)')
    return { 'name': name_pattern.search(content).group(1) }


parser = argparse.ArgumentParser(
    description='Unpack a WHL file as a py_library.')

parser.add_argument('--whl', action='store',
                    help=('The .whl file we are expanding.'))

parser.add_argument('--requirements', action='store',
                    help='The pip_import from which to draw dependencies.')

parser.add_argument('--directory', action='store', default='.',
                    help='The directory into which to expand things.')

parser.add_argument('--extras', action='append',
                    help='The set of extras for which to generate library targets.')

def main():
  args = parser.parse_args()
  whl = Wheel(args.whl)

  # Extract the files into the current directory
  whl.expand(args.directory)

  with open(os.path.join(args.directory, 'BUILD'), 'w') as f:
    f.write("""
package(default_visibility = ["//visibility:public"])

load("@rules_python//python:defs.bzl", "py_library")
load("{requirements}", "requirement")

py_library(
    name = "pkg",
    srcs = glob(["**/*.py"]),
    data = glob(["**/*"], exclude=["**/*.py", "**/* *", "BUILD", "WORKSPACE"]),
    # This makes this directory a top-level in the python import
    # search path for anything that depends on this.
    imports = ["."],
    deps = [{dependencies}],
)
{extras}""".format(
  requirements=args.requirements,
  dependencies=','.join([
    'requirement("%s")' % d
    for d in whl.dependencies()
  ]),
  extras='\n\n'.join([
    """py_library(
    name = "{extra}",
    deps = [
        ":pkg",{deps}
    ],
)""".format(extra=extra,
            deps=','.join([
                'requirement("%s")' % dep
                for dep in whl.dependencies(extra)
            ]))
    for extra in args.extras or []
  ])))

if __name__ == '__main__':
  main()
