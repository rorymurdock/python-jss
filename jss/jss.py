#!/usr/bin/env python
# Copyright (C) 2014, 2015 Shea G Craig <shea.craig@da.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""jss.py

Classes representing a JSS, and its available API calls, represented
as JSSObjects.
"""


import copy
import os
import re
import ssl
import subprocess
from urllib import quote
import urlparse
from xml.dom import minidom
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError

import requests

from . import distribution_points
from .exceptions import (
    JSSPrefsMissingFileError, JSSPrefsMissingKeyError, JSSGetError,
    JSSPutError, JSSPostError, JSSDeleteError, JSSMethodNotAllowedError,
    JSSUnsupportedSearchMethodError, JSSFileUploadParameterError)
from .tlsadapter import TLSAdapter
from .tools import is_osx, is_linux

try:
    from .contrib import FoundationPlist
except ImportError as err:
    # If using OSX, FoundationPlist will need Foundation/PyObjC
    # available, or it won't import.

    if is_osx():
        print "Warning: Import of FoundationPlist failed:", err
        print "See README for information on this issue."
    import plistlib


class JSSPrefs(object):
    """Object representing JSS credentials and configuration.

    This JSSPrefs object can be used as an argument for a new JSS.
    By default and with no arguments, it uses the preference domain
    "com.github.sheagcraig.python-jss.plist". However, alternate
    configurations can be supplied to the __init__ method to use
    something else.

    Preference file should include the following keys:
        jss_url: String, full path, including port, to JSS, e.g.
            "https://mycasper.donkey.com:8443".
        jss_user: String, API username to use.
        jss_pass: String, API password.
        verify: (Optional) Boolean for whether to verify the JSS's
            certificate matches the SSL traffic. This certificate must
            be in your keychain. Defaults to True.
        repos: (Optional) A list of file repositories dicts to connect.
        repos dicts:
            Each file-share distribution point requires:
            name: String name of the distribution point. Must match
                the value on the JSS.
            password: String password for the read/write user.

            This form uses the distributionpoints API call to determine
            the remaining information. There is also an explicit form;
            See distribution_points package for more info

            CDP and JDS types require one dict for the master, with
            key:
                type: String, either "CDP" or "JDS".
    """

    def __init__(self, preferences_file=None):
        """Create a preferences object.

        This JSSPrefs object can be used as an argument for a new JSS.
        By default and with no arguments, it uses the preference domain
        "com.github.sheagcraig.python-jss.plist". However, alternate
        configurations can be supplied to the __init__ method to use
        something else.

        See the JSSPrefs __doc__ for information on supported
        preferences.

        Args:
            preferences_file: String path to an alternate location to
                look for preferences.

        Raises:
            JSSError if using an unsupported OS.
        """
        if preferences_file is None:
            plist_name = "com.github.sheagcraig.python-jss.plist"
            if is_osx():
                preferences_file = os.path.join("~", "Library", "Preferences",
                                                plist_name)
            elif is_linux():
                preferences_file = os.path.join("~", "." + plist_name)
            else:
                raise JSSError("Unsupported OS.")

        preferences_file = os.path.expanduser(preferences_file)
        if os.path.exists(preferences_file):
            # Try to open using FoundationPlist. If it's not available,
            # fall back to plistlib and hope it's not binary encoded.

            try:
                prefs = FoundationPlist.readPlist(preferences_file)
            except NameError:
                try:
                    prefs = plistlib.readPlist(preferences_file)
                except ExpatError:
                    # If we're on OSX, try to convert using another
                    # tool.

                    if is_osx():
                        subprocess.call(
                            ["plutil", "-convert", "xml1", preferences_file])
                        prefs = plistlib.readPlist(preferences_file)
            try:
                self.user = prefs["jss_user"]
                self.password = prefs["jss_pass"]
                self.url = prefs["jss_url"]
            except KeyError:
                raise JSSPrefsMissingKeyError("Please provide all required "
                                              "preferences!")

            # Optional file repository array. Defaults to empty list.
            self.repos = []
            for repo in prefs.get("repos", []):
                self.repos.append(dict(repo))

            self.verify = prefs.get("verify", True)

        else:
            raise JSSPrefsMissingFileError("Preferences file not found!")


class JSS(object):
    """Represents a JAMF Software Server, with object search methods.

    Attributes:
        base_url: String, full URL to the JSS, with port.
        user: String API username.
        password: String API password for user.
        repo_prefs: List of dicts of repository configuration data.
        verbose: Boolean whether to include extra output.
        jss_migrated: Boolean whether JSS has had scripts "migrated".
            Used to determine whether to upload scripts in Script
            object XML or as files to the distribution points.
        session: Requests session used to make all HTTP requests.
        ssl_verify: Boolean whether to verify SSL traffic from the JSS
            is genuine.
        factory: JSSObjectFactory object for building JSSObjects.
        distribution_points: DistributionPoints
    """

    def __init__(self, jss_prefs=None, url=None, user=None, password=None,
                 repo_prefs=None, ssl_verify=True, verbose=False,
                 jss_migrated=False, suppress_warnings=False):
        """Setup a JSS for making API requests.

        Provide either a JSSPrefs object OR specify url, user, and
        password to init. Other parameters are optional.

        Args:
            jss_prefs:  A JSSPrefs object.
            url: String, full URL to a JSS, with port.
            user: API Username.
            password: API Password.

            repo_prefs: A list of dicts with repository names and
                passwords.
            repos: (Optional) List of file repositories dicts to
                    connect.
                repo dicts:
                    Each file-share distribution point requires:
                        name: String name of the distribution point.
                            Must match the value on the JSS.
                        password: String password for the read/write
                            user.

                    This form uses the distributionpoints API call to
                    determine the remaining information. There is also
                    an explicit form; See distribution_points package
                    for more info

                    CDP and JDS types require one dict for the master,
                    with key:
                        type: String, either "CDP" or "JDS".

            ssl_verify: Boolean whether to verify SSL traffic from the
                JSS is genuine.
            verbose: Boolean whether to include extra output.
            jss_migrated: Boolean whether JSS has had scripts
                "migrated". Used to determine whether to upload scripts
                in Script object XML or as files to the distribution
                points.
            suppress_warnings: Turns off the urllib3 warnings. Remember,
                these warnings are there for a reason! Use at your own
                risk.
        """
        if jss_prefs is not None:
            url = jss_prefs.url
            user = jss_prefs.user
            password = jss_prefs.password
            repo_prefs = jss_prefs.repos
            ssl_verify = jss_prefs.verify

        if suppress_warnings:
            requests.packages.urllib3.disable_warnings()

        self.base_url = url
        self.user = user
        self.password = password
        self.repo_prefs = repo_prefs if repo_prefs else []
        self.verbose = verbose
        self.jss_migrated = jss_migrated
        self.session = requests.Session()
        self.session.auth = (self.user, self.password)
        self.ssl_verify = ssl_verify

        # For some objects the JSS tries to return JSON, so we explictly
        # request XML.

        headers = {"content-type": "text/xml", "Accept": "application/xml"}
        self.session.headers.update(headers)

        # Add a TransportAdapter to force TLS, since JSS no longer
        # accepts SSLv23, which is the default.

        self.session.mount(self.base_url, TLSAdapter())

        self.factory = JSSObjectFactory(self)
        self.distribution_points = distribution_points.DistributionPoints(self)

    def _error_handler(self, exception_cls, response):
        """Generic error handler. Converts html responses to friendlier
        text.

        """
        # Responses are sent as html. Split on the newlines and give us
        # the <p> text back.
        errorlines = response.text.encode("utf-8").split("\n")
        error = []
        for line in errorlines:
            e = re.search(r"<p.*>(.*)</p>", line)
            if e:
                error.append(e.group(1))

        error = ". ".join(error)
        exception = exception_cls("Response Code: %s\tResponse: %s"
                                  % (response.status_code, error))
        exception.status_code = response.status_code
        raise exception

    @property
    def _url(self):
        """The URL to the Casper JSS API endpoints. Get only."""
        return "%s/%s" % (self.base_url, "JSSResource")

    @property
    def base_url(self):
        """The URL to the Casper JSS, including port if needed."""
        return self._base_url

    @base_url.setter
    def base_url(self, url):
        """The URL to the Casper JSS, including port if needed."""
        # Remove the frequently included yet incorrect trailing slash.
        self._base_url = url.rstrip("/")

    @property
    def ssl_verify(self):
        """Boolean value for whether to verify SSL traffic."""
        return self.session.verify

    @ssl_verify.setter
    def ssl_verify(self, value):
        """Boolean value for whether to verify SSL traffic.

        Args:
            value: Boolean.
        """
        self.session.verify = value

    def get(self, url_path):
        """Get a url, handle errors, and return an etree from the XML
        data.

        """
        request_url = "%s%s" % (self._url, quote(url_path.encode("utf_8")))
        response = self.session.get(request_url)

        if response.status_code == 200:
            if self.verbose:
                print("GET: Success.")
        elif response.status_code >= 400:
            self._error_handler(JSSGetError, response)

        # JSS returns xml encoded in utf-8
        jss_results = response.text.encode("utf-8")
        try:
            xmldata = ElementTree.fromstring(jss_results)
        except ElementTree.ParseError:
            raise JSSGetError("Error Parsing XML:\n%s" % jss_results)
        return xmldata

    def post(self, obj_class, url_path, data):
        """Post an object to the JSS. For creating new objects only."""
        # The JSS expects a post to ID 0 to create an object
        request_url = "%s%s" % (self._url, url_path)
        data = ElementTree.tostring(data)
        response = self.session.post(request_url, data=data)

        if response.status_code == 201:
            if self.verbose:
                print("POST: Success")
        elif response.status_code >= 400:
            self._error_handler(JSSPostError, response)

        # Get the ID of the new object. JSS returns xml encoded in utf-8
        jss_results = response.text.encode("utf-8")
        id_ = int(re.search(r"<id>([0-9]+)</id>", jss_results).group(1))

        return self.factory.get_object(obj_class, id_)

    def put(self, url_path, data):
        """Updates an object on the JSS."""
        request_url = "%s%s" % (self._url, url_path)
        data = ElementTree.tostring(data)
        response = self.session.put(request_url, data)

        if response.status_code == 201:
            if self.verbose:
                print("PUT: Success.")
        elif response.status_code >= 400:
            self._error_handler(JSSPutError, response)

    def delete(self, url_path):
        """Delete an object from the JSS."""
        request_url = "%s%s" % (self._url, url_path)
        response = self.session.delete(request_url)

        if response.status_code == 200:
            if self.verbose:
                print("DEL: Success.")
        elif response.status_code >= 400:
            self._error_handler(JSSDeleteError, response)

    # Factory methods for all JSSObject types ##########################

    def Account(self, data=None):
        return self.factory.get_object(Account, data)

    def AccountGroup(self, data=None):
        return self.factory.get_object(AccountGroup, data)

    def AdvancedComputerSearch(self, data=None):
        return self.factory.get_object(AdvancedComputerSearch, data)

    def AdvancedMobileDeviceSearch(self, data=None):
        return self.factory.get_object(AdvancedMobileDeviceSearch, data)

    def AdvancedUserSearch(self, data=None):
        return self.factory.get_object(AdvancedUserSearch, data)

    def ActivationCode(self, data=None):
        return self.factory.get_object(ActivationCode, data)

    def Building(self, data=None):
        return self.factory.get_object(Building, data)

    def BYOProfile(self, data=None):
        return self.factory.get_object(BYOProfile, data)

    def Category(self, data=None):
        return self.factory.get_object(Category, data)

    def Class(self, data=None):
        return self.factory.get_object(Class, data)

    def Computer(self, data=None, subset=None):
        return self.factory.get_object(Computer, data, subset)

    def ComputerCheckIn(self, data=None):
        return self.factory.get_object(ComputerCheckIn, data)

    def ComputerCommand(self, data=None):
        return self.factory.get_object(ComputerCommand, data)

    def ComputerConfiguration(self, data=None):
        return self.factory.get_object(ComputerConfiguration, data)

    def ComputerExtensionAttribute(self, data=None):
        return self.factory.get_object(ComputerExtensionAttribute, data)

    def ComputerGroup(self, data=None):
        return self.factory.get_object(ComputerGroup, data)

    def ComputerInventoryCollection(self, data=None):
        return self.factory.get_object(ComputerInventoryCollection, data)

    def ComputerInvitation(self, data=None):
        return self.factory.get_object(ComputerInvitation, data)

    def ComputerReport(self, data=None):
        return self.factory.get_object(ComputerReport, data)

    def Department(self, data=None):
        return self.factory.get_object(Department, data)

    def DirectoryBinding(self, data=None):
        return self.factory.get_object(DirectoryBinding, data)

    def DiskEncryptionConfiguration(self, data=None):
        return self.factory.get_object(DiskEncryptionConfiguration, data)

    def DistributionPoint(self, data=None):
        return self.factory.get_object(DistributionPoint, data)

    def DockItem(self, data=None):
        return self.factory.get_object(DockItem, data)

    def EBook(self, data=None, subset=None):
        return self.factory.get_object(EBook, data, subset)

    # FileUploads' only function is to upload, so a method here is not
    # provided.

    def GSXConnection(self, data=None):
        return self.factory.get_object(GSXConnection, data)

    def IBeacon(self, data=None):
        return self.factory.get_object(IBeacon, data)

    def JSSUser(self, data=None):
        return self.factory.get_object(JSSUser, data)

    def LDAPServer(self, data=None):
        return self.factory.get_object(LDAPServer, data)

    def LicensedSoftware(self, data=None):
        return self.factory.get_object(LicensedSoftware, data)

    def MacApplication(self, data=None, subset=None):
        return self.factory.get_object(MacApplication, data, subset)

    def ManagedPreferenceProfile(self, data=None, subset=None):
        return self.factory.get_object(ManagedPreferenceProfile, data, subset)

    def MobileDevice(self, data=None, subset=None):
        return self.factory.get_object(MobileDevice, data, subset)

    def MobileDeviceApplication(self, data=None, subset=None):
        return self.factory.get_object(MobileDeviceApplication, data, subset)

    def MobileDeviceCommand(self, data=None):
        return self.factory.get_object(MobileDeviceCommand, data)

    def MobileDeviceConfigurationProfile(self, data=None, subset=None):
        return self.factory.get_object(MobileDeviceConfigurationProfile, data,
                                       subset)

    def MobileDeviceEnrollmentProfile(self, data=None, subset=None):
        return self.factory.get_object(MobileDeviceEnrollmentProfile, data,
                                       subset)

    def MobileDeviceExtensionAttribute(self, data=None):
        return self.factory.get_object(MobileDeviceExtensionAttribute, data)

    def MobileDeviceInvitation(self, data=None):
        return self.factory.get_object(MobileDeviceInvitation, data)

    def MobileDeviceGroup(self, data=None):
        return self.factory.get_object(MobileDeviceGroup, data)

    def MobileDeviceProvisioningProfile(self, data=None, subset=None):
        return self.factory.get_object(MobileDeviceProvisioningProfile, data,
                                       subset)

    def NetbootServer(self, data=None):
        return self.factory.get_object(NetbootServer, data)

    def NetworkSegment(self, data=None):
        return self.factory.get_object(NetworkSegment, data)

    def OSXConfigurationProfile(self, data=None, subset=None):
        return self.factory.get_object(OSXConfigurationProfile, data, subset)

    def Package(self, data=None):
        return self.factory.get_object(Package, data)

    def Peripheral(self, data=None, subset=None):
        return self.factory.get_object(Peripheral, data, subset)

    def PeripheralType(self, data=None):
        return self.factory.get_object(PeripheralType, data)

    def Policy(self, data=None, subset=None):
        return self.factory.get_object(Policy, data, subset)

    def Printer(self, data=None):
        return self.factory.get_object(Printer, data)

    def RestrictedSfotware(self, data=None):
        return self.factory.get_object(RestrictedSoftware, data)

    def RemovableMACAddress(self, data=None):
        return self.factory.get_object(RemovableMACAddress, data)

    def SavedSearch(self, data=None):
        return self.factory.get_object(SavedSearch, data)

    def Script(self, data=None):
        return self.factory.get_object(Script, data)

    def Site(self, data=None):
        return self.factory.get_object(Site, data)

    def SoftwareUpdateServer(self, data=None):
        return self.factory.get_object(SoftwareUpdateServer, data)

    def SMTPServer(self, data=None):
        return self.factory.get_object(SMTPServer, data)

    def UserExtensionAttribute(self, data=None):
        return self.factory.get_object(UserExtensionAttribute, data)

    def User(self, data=None):
        return self.factory.get_object(User, data)

    def UserGroup(self, data=None):
        return self.factory.get_object(UserGroup, data)

    def VPPAccount(self, data=None):
        return self.factory.get_object(VPPAccount, data)


class JSSObjectFactory(object):
    """Create JSSObjects intelligently based on a single data
    argument.

    """
    def __init__(self, jss):
        self.jss = jss

    def get_object(self, obj_class, data=None, subset=None):
        """Return a subclassed JSSObject instance by querying for
        existing objects or posting a new object. List operations return
        a JSSObjectList.

        obj_class is the class to retrieve.
        data is flexible.
            If data is type:
                None:   Perform a list operation, or for non-container
                        objects, return all data.
                int:    Retrieve an object with ID of <data>
                str:    Retrieve an object with name of <str>. For some
                        objects, this may be overridden to include
                        searching by other criteria. See those objects
                        for more info.
                dict:   Get the existing object with <dict>["id"]
                xml.etree.ElementTree.Element:
                        Create a new object from xml

                Warning! Be sure to pass ID's as ints, not str!
        subset:
            A list of sub-tags to request, or an "&" delimited string,
            (e.g. "general&purchasing").
        """
        if subset:
            if not isinstance(subset, list):
                if isinstance(subset, (str, unicode)):
                    subset = subset.split("&")
                else:
                    raise TypeError

        # List objects
        if data is None:
            url = obj_class.get_url(data)
            if obj_class.can_list and obj_class.can_get:
                if (subset and len(subset) == 1 and subset[0].upper() ==
                    "BASIC") and obj_class is Computer:
                    url += "/subset/basic"
                result = self.jss.get(url)
                if obj_class.container:
                    result = result.find(obj_class.container)
                response_objects = [item for item in result if item is not None
                                    and item.tag != "size"]
                objects = [JSSListData(obj_class,
                                       {i.tag: i.text for i
                                        in response_object})
                           for response_object in response_objects]

                return JSSObjectList(self, obj_class, objects)
            elif obj_class.can_get:
                # Single object
                xmldata = self.jss.get(url)
                return obj_class(self.jss, xmldata)
            else:
                raise JSSMethodNotAllowedError(obj_class.__class__.__name__)
        # Retrieve individual objects
        elif type(data) in [str, int, unicode]:
            if obj_class.can_get:
                url = obj_class.get_url(data)
                if subset:
                    if not "general" in subset:
                        subset.append("general")
                    url += "/subset/%s" % "&".join(subset)
                xmldata = self.jss.get(url)
                if xmldata.find("size") is not None:
                    # May need above treatment, with .find(container),
                    # and refactoring out this otherwise duplicate code.

                    # Get returned a list.
                    response_objects = [item for item in xmldata
                                        if item is not None and
                                        item.tag != "size"]
                    objects = [JSSListData(obj_class,
                                           {i.tag: i.text for i
                                            in response_object})
                               for response_object in response_objects]

                    return JSSObjectList(self, obj_class, objects)
                else:
                    return obj_class(self.jss, xmldata)
            else:
                raise JSSMethodNotAllowedError(obj_class.__class__.__name__)
        # Create a new object
        # elif isinstance(data, JSSObjectTemplate):
        #     if obj_class.can_post:
        #         url = obj_class.get_post_url()
        #         return self.jss.post(obj_class, url, data)
        #     else:
        #         raise JSSMethodNotAllowedError(obj_class.__class__.__name__)


class JSSObject(ElementTree.Element):
    """Base class for representing all available JSS API objects.

    """
    _url = None
    can_list = True
    can_get = True
    can_put = True
    can_post = True
    can_delete = True
    id_url = "/id/"
    container = ""
    default_search = "name"
    search_types = {"name": "/name/"}
    list_type = "JSSObject"

    def __init__(self, jss, data, **kwargs):
        """Initialize a new JSSObject

        jss:    JSS object.
        data:   Valid XML.

        """
        if not isinstance(jss, JSS):
            raise TypeError("Argument jss must be an instance of JSS.")
        self.jss = jss
        if type(data) in [str, unicode]:
            super(JSSObject, self).__init__(tag=self.list_type)
            self.new(data, **kwargs)
        elif isinstance(data, ElementTree.Element):
            super(JSSObject, self).__init__(tag=data.tag)
            for child in data.getchildren():
                self.append(child)
        else:
            raise TypeError("JSSObjects data argument must be of type "
                            "xml.etree.ElemenTree.Element, or a string for the"
                            " name.")

    def new(self, name, **kwargs):
        raise NotImplementedError

    def makeelement(self, tag, attrib):
        """Return an Element."""
        # We use ElementTree.SubElement() a lot. Unfortunately, it
        # relies on a super() call to its __class__.makeelement(), which
        # will fail due to the class NOT being Element.
        # This handles that issue.
        return ElementTree.Element(tag, attrib)

    @classmethod
    def get_url(cls, data):
        """Return the URL for a get request based on data type."""
        # Test for a string representation of an integer
        try:
            data = int(data)
        except (ValueError, TypeError):
            pass
        if isinstance(data, int):
            return "%s%s%s" % (cls._url, cls.id_url, data)
        elif data is None:
            return cls._url
        else:
            if "=" in data:
                key, value = data.split("=")
                if key in cls.search_types:
                    return "%s%s%s" % (cls._url, cls.search_types[key], value)
                else:
                    raise JSSUnsupportedSearchMethodError(
                        "This object cannot be queried by %s." % key)
            else:
                return "%s%s%s" % (cls._url,
                                   cls.search_types[cls.default_search], data)

    @classmethod
    def get_post_url(cls):
        """Return the post URL for this object class."""
        return "%s%s%s" % (cls._url, cls.id_url, "0")

    def get_object_url(self):
        """Return the complete API url to this object."""
        return "%s%s%s" % (self._url, self.id_url, self.id)

    def delete(self):
        """Delete this object from the JSS."""
        if not self.can_delete:
            raise JSSMethodNotAllowedError(self.__class__.__name__)
        self.jss.delete(self.get_object_url())

    def save(self):
        """Update existing objects or create new object on the JSS.

        Data validation is up to the client.

        """
        # If obj can't PUT or POST, stop here.
        if not self.can_put and not self.can_post:
            raise JSSMethodNotAllowedError(self.__class__.__name__)
        # Most objects can both PUT and POST. This block handles them.
        # It will also handle those which can only PUT.
        elif self.can_put:
            url = self.get_object_url()
            try:
                self.jss.put(url, self)
                updated_data = self.jss.get(url)
            except JSSPutError as put_error:
                # Object doesn't exist, try creating a new one.
                if put_error.status_code == 404:
                    if self.can_post:
                        url = self.get_post_url()
                        try:
                            updated_data = self.jss.post(self.__class__, url,
                                                         self)
                        except JSSPostError as e:
                            raise JSSPostError(e)
                    else:
                        raise JSSMethodNotAllowedError(self.__class__.__name__)
                else:
                    # Something else went wrong
                    raise JSSPutError(put_error)

        # Finally, handle those which can only POST.
        elif not self.can_put and self.can_post:
            url = self.get_post_url()
            try:
                updated_data = self.jss.post(self.__class__, url, self)
            except JSSPostError as e:
                raise JSSPostError(e)

        # If successful, replace current instance's data with new,
        # JSS-filled data.
        self.clear()
        for child in updated_data.getchildren():
            self._children.append(child)

    # Shared properties:
    # Almost all JSSObjects have at least name and id properties, so
    # provide a convenient accessor.
    @property
    def name(self):
        """Return object name or None."""
        return self.findtext("name") or self.findtext("general/name")

    @property
    def id(self):
        """Return object ID or None."""
        # Most objects have ID nested in general. Groups don't.
        result = self.findtext("id") or self.findtext("general/id")
        # After much consideration, I will treat id's as strings.
        #   We can't assign ID's, so there's no need to perform
        #   arithmetic on them.  Having to convert to str all over the
        #   place is gross.  str equivalency still works.
        return result

    def _indent(self, elem, level=0, more_sibs=False):
        """Indent an xml element object to prepare for pretty printing.

        Method is internal to discourage indenting the self._root
        Element, thus potentially corrupting it.

        """
        i = "\n"
        pad = "    "
        if level:
            i += (level - 1) * pad
        num_kids = len(elem)
        if num_kids:
            if not elem.text or not elem.text.strip():
                elem.text = i + pad
                if level:
                    elem.text += pad
            count = 0
            for kid in elem:
                if kid.tag == "data":
                    kid.text = "*DATA*"
                self._indent(kid, level+1, count < num_kids - 1)
                count += 1
            if not elem.tail or not elem.tail.strip():
                elem.tail = i
                if more_sibs:
                    elem.tail += pad
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = i
                if more_sibs:
                    elem.tail += pad

    def __repr__(self):
        """Make our data human readable."""
        # deepcopy so we don't mess with the valid XML.
        pretty_data = copy.deepcopy(self)
        self._indent(pretty_data)
        elementstring = ElementTree.tostring(pretty_data)
        return elementstring.encode("utf-8")

    def pretty_find(self, search):
        """Pretty print the results of a find.

        Args:
            search: xpath passed onto the find method.
        """
        result = self.find(search)
        if result is not None:
            pretty_data = copy.deepcopy(result)
            self._indent(pretty_data)
            elementstring = ElementTree.tostring(pretty_data)
            print elementstring.encode("utf-8")

    def _handle_location(self, location):
        """Return an element located at location.

        Handles a string xpath as per ElementTree.find or an element.

        """
        if not isinstance(location, ElementTree.Element):
            element = self.find(location)
            if element is None:
                raise ValueError("Invalid path!")
        else:
            element = location
        return element

    def search(self, tag):
        """Return elements with tag using getiterator."""
        # TODO: getiterator is deprecated, and I'm not sure why this
        # function is even here any more!
        return self.getiterator(tag)

    def set_bool(self, location, value):
        """For an object at path, set the string representation of a
        boolean value to value. Mostly just to prevent me from
        forgetting to convert to string.

        """
        element = self._handle_location(location)
        if bool(value) is True:
            element.text = "true"
        else:
            element.text = "false"

    def add_object_to_path(self, obj, location):
        """Add an object of type JSSContainerObject to XMLEditor's
        context object at "path".

        location can be an Element or a string path argument to find()

        """
        location = self._handle_location(location)
        location.append(obj.as_list_data())
        results = [item for item in location.getchildren() if
                   item.findtext("id") == obj.id][0]
        return results

    def remove_object_from_list(self, obj, list_element):
        """Remove an object from a list element.

        object:     Accepts JSSObjects, id's, and names
        list:   Accepts an element or a string path to that element

        """
        list_element = self._handle_location(list_element)

        if isinstance(obj, JSSObject):
            results = [item for item in list_element.getchildren() if
                       item.findtext("id") == obj.id]
        elif type(obj) in [int, str, unicode]:
            results = [item for item in list_element.getchildren() if
                       item.findtext("id") == str(obj) or
                       item.findtext("name") == obj]

        if len(results) == 1:
            list_element.remove(results[0])
        else:
            raise ValueError("There is either more than one object, or no "
                             "matches at that path!")

    def clear_list(self, list_element):
        """Clear all list items from path.

        list_element can be a string argument to find(), or an element.

        """
        list_element = self._handle_location(list_element)
        list_element.clear()

    @classmethod
    def from_file(cls, jss, filename):
        """Creates a new JSSObject from an external XML file."""
        tree = ElementTree.parse(filename)
        root = tree.getroot()
        new_object = cls(jss, data=root)
        return new_object

    @classmethod
    def from_string(cls, jss, xml_string):
        """Creates a new JSSObject from an XML string."""
        root = ElementTree.fromstring(xml_string)
        new_object = cls(jss, data=root)
        return new_object


class JSSContainerObject(JSSObject):
    """Subclass for object types which can contain lists.

    e.g. Computers, Policies.

    """
    list_type = "JSSContainerObject"

    def new(self, name, **kwargs):
        name_element = ElementTree.SubElement(self, "name")
        name_element.text = name

    def as_list_data(self):
        """Return an Element with id and name data for adding to
        lists.

        """
        element = ElementTree.Element(self.list_type)
        id_ = ElementTree.SubElement(element, "id")
        id_.text = self.id
        name = ElementTree.SubElement(element, "name")
        name.text = self.name
        return element


class JSSGroupObject(JSSContainerObject):
    """Abstract XMLEditor for ComputerGroup and MobileDeviceGroup."""

    def add_criterion(self, name, priority, and_or, search_type, value):
        """Add a search criteria object to a smart group."""
        criterion = SearchCriteria(name, priority, and_or, search_type, value)
        self.criteria.append(criterion)

    @property
    def is_smart(self):
        """Returns boolean for whether group is Smart."""
        result = False
        if self.findtext("is_smart") == "true":
            result = True
        return result

    @is_smart.setter
    def is_smart(self, value):
        """Set group is_smart property to value.

        Args:
            value: Boolean.
        """
        self.set_bool("is_smart", value)
        if value is True:
            if self.find("criteria") is None:
                self.criteria = ElementTree.SubElement(self, "criteria")

    def add_device(self, device, container):
        """Add a device to a group. Wraps XMLEditor.add_object_toPath.

        device can be a JSSObject, and ID value, or the name of a valid
        object.

        """
        # There is a size tag which the JSS manages for us, so we can
        # ignore it.
        if self.findtext("is_smart") == "false":
            self.add_object_to_path(device, container)
        else:
            # Technically this isn't true. It will strangely accept
            # them, and they even show up as members of the group!
            raise ValueError("Devices may not be added to smart groups.")

    def has_member(self, device_object):
        """Return whether group has a device as a member.

        Args:
            Device object (Computer or MobileDevice). Membership is
            determined by ID, as names can be shared amongst devices.
        """
        if isinstance(device_object, Computer):
            container_search = "computers/computer"
        elif isinstance(device_object, MobileDevice):
            container_search = "mobile_devices/mobile_device"
        else:
            raise ValueError

        return len([device for device in self.findall(container_search) if
                    device.findtext("id") == device_object.id]) is not 0


class JSSDeviceObject(JSSContainerObject):
    """Provides convenient accessors for properties of devices.

    This is helpful since Computers and MobileDevices allow us to query
    based on these properties.

    """
    @property
    def udid(self):
        """Return device's UDID or None."""
        return self.findtext("general/udid")

    @property
    def serial_number(self):
        """Return device's serial number or None."""
        return self.findtext("general/serial_number")


class JSSFlatObject(JSSObject):
    """Subclass for JSS objects which do not return a list of objects.

    These objects have in common that they cannot be created. They can,
    however, be updated.

    """
    search_types = {}

    def new(self, name, **kwargs):
        """JSSFlatObjects and their subclasses cannot be created."""
        raise JSSPostError("This object type cannot be created.")

    @classmethod
    def get_url(cls, data):
        """Return the URL for a get request based on data type."""
        if data is not None:
            raise JSSUnsupportedSearchMethodError(
                "This object cannot be queried by %s." % data)
        else:
            return cls._url

    def get_object_url(self):
        """Return the complete API url to this object."""
        return self.get_url(None)


class Account(JSSContainerObject):
    _url = "/accounts"
    container = "users"
    id_url = "/userid/"
    search_types = {"userid": "/userid/", "username": "/username/",
                    "name": "/username/"}


class AccountGroup(JSSContainerObject):
    """Account groups are groups of users on the JSS. Within the API
    hierarchy they are actually part of accounts, but I seperated them.

    """
    _url = "/accounts"
    container = "groups"
    id_url = "/groupid/"
    search_types = {"groupid": "/groupid/", "groupname": "/groupname/",
                    "name": "/groupname/"}


class ActivationCode(JSSFlatObject):
    _url = "/activationcode"
    list_type = "activation_code"
    can_delete = False
    can_post = False
    can_list = False


class AdvancedComputerSearch(JSSContainerObject):
    _url = "/advancedcomputersearches"


class AdvancedMobileDeviceSearch(JSSContainerObject):
    _url = "/advancedmobiledevicesearches"


class AdvancedUserSearch(JSSContainerObject):
    _url = "/advancedusersearches"


class Building(JSSContainerObject):
    _url = "/buildings"
    list_type = "building"


class BYOProfile(JSSContainerObject):
    _url = "/byoprofiles"
    list_type = "byoprofiles"
    can_delete = False
    can_post = False


class Category(JSSContainerObject):
    _url = "/categories"
    list_type = "category"


class Class(JSSContainerObject):
    _url = "/classes"


class Computer(JSSDeviceObject):
    """Computer objects include a "match" search type which queries
    across multiple properties.

    """
    list_type = "computer"
    _url = "/computers"
    search_types = {"name": "/name/", "serial_number": "/serialnumber/",
                    "udid": "/udid/", "macaddress": "/macadress/",
                    "match": "/match/"}

    @property
    def mac_addresses(self):
        """Return a list of mac addresses for this device."""
        # Computers don't tell you which network device is which.
        mac_addresses = [self.findtext("general/mac_address")]
        if self.findtext("general/alt_mac_address"):
            mac_addresses.append(self.findtext("general/alt_mac_address"))
            return mac_addresses


class ComputerCheckIn(JSSFlatObject):
    _url = "/computercheckin"
    can_delete = False
    can_list = False
    can_post = False


class ComputerCommand(JSSContainerObject):
    _url = "/computercommands"
    can_delete = False
    can_put = False


class ComputerConfiguration(JSSContainerObject):
    _url = "/computerconfigurations"
    list_type = "computer_configuration"


class ComputerExtensionAttribute(JSSContainerObject):
    _url = "/computerextensionattributes"


class ComputerGroup(JSSGroupObject):
    _url = "/computergroups"
    list_type = "computer_group"

    def __init__(self, jss, data, **kwargs):
        """Init a ComputerGroup, adding in extra Elements."""
        # Temporary solution to #34.
        # When grabbing a ComputerGroup from the JSS, we don't get the
        # convenience properties for accessing some of the elements
        # that the new() method adds. For now, this just adds in a
        # criteria property. But...
        # TODO(Shea): Find a generic/higher level way to add these
        #   convenience accessors.
        super(ComputerGroup, self).__init__(jss, data, **kwargs)
        self.criteria = self.find("criteria")

    def new(self, name, **kwargs):
        """Creates a computer group template.

        Smart groups with no criteria by default select ALL computers.

        """
        element_name = ElementTree.SubElement(self, "name")
        element_name.text = name
        # is_smart is a JSSGroupObject @property.
        ElementTree.SubElement(self, "is_smart")
        self.criteria = ElementTree.SubElement(self, "criteria")
        # Assing smartness if specified, otherwise default to False.
        self.is_smart = kwargs.get("smart", False)
        self.computers = ElementTree.SubElement(self, "computers")

    def add_computer(self, device):
        """Add a computer to the group."""
        super(ComputerGroup, self).add_device(device, "computers")

    def remove_computer(self, device):
        """Remove a computer from the group."""
        super(ComputerGroup, self).remove_object_from_list(device, "computers")


class ComputerInventoryCollection(JSSFlatObject):
    _url = "/computerinventorycollection"
    can_list = False
    can_post = False
    can_delete = False


class ComputerInvitation(JSSContainerObject):
    _url = "/computerinvitations"
    can_put = False
    search_types = {"name": "/name/", "invitation": "/invitation/"}


class ComputerReport(JSSContainerObject):
    _url = "/computerreports"
    can_put = False
    can_post = False
    can_delete = False


class Department(JSSContainerObject):
    _url = "/departments"
    list_type = "department"


class DirectoryBinding(JSSContainerObject):
    _url = "/directorybindings"


class DiskEncryptionConfiguration(JSSContainerObject):
    _url = "/diskencryptionconfigurations"


class DistributionPoint(JSSContainerObject):
    _url = "/distributionpoints"


class DockItem(JSSContainerObject):
    _url = "/dockitems"


class EBook(JSSContainerObject):
    _url = "/ebooks"


class FileUpload(object):
    """FileUploads are a special case in the API. They allow you to add
    file resources to a number of objects on the JSS.

    To use, instantiate a new FileUpload object, then use the save()
    method to upload.

    Once the upload has been posted you may only interact with it
    through the web interface. You cannot list/get it or delete it
    through the API.

    However, you can reuse the FileUpload object if you wish, by
    changing the parameters, and issuing another save().

    """
    _url = "fileuploads"

    def __init__(self, j, resource_type, id_type, _id, resource):
        """Prepare a new FileUpload.

        j:                  A JSS object to POST the upload to.

        resource_type:      String. Acceptable Values:
                            Attachments:
                                computers
                                mobiledevices
                                enrollmentprofiles
                                peripherals
                            Icons:
                                policies
                                ebooks
                                mobiledeviceapplicationsicon
                            Mobile Device Application:
                                mobiledeviceapplicationsipa
                            Disk Encryption
                                diskencryptionconfigurations
        id_type:            String of desired ID type:
                                id
                                name

        _id                 Int or String referencing the identity value
                            of the resource to add the FileUpload to.

        resource            String path to the file to upload.

        """
        resource_types = ["computers", "mobiledevices", "enrollmentprofiles",
                          "peripherals", "policies", "ebooks",
                          "mobiledeviceapplicationsicon",
                          "mobiledeviceapplicationsipa",
                          "diskencryptionconfigurations"]
        id_types = ["id", "name"]

        self.jss = j

        # Do some basic error checking on parameters.
        if resource_type in resource_types:
            self.resource_type = resource_type
        else:
            raise JSSFileUploadParameterError("resource_type must be one of: "
                                              "%s" % resource_types)
        if id_type in id_types:
            self.id_type = id_type
        else:
            raise JSSFileUploadParameterError("id_type must be one of: "
                                              "%s" % id_types)
        self._id = str(_id)

        # To support curl workaround in FileUpload.save()...
        self.resource_path = resource

        self.resource = {"name": (os.path.basename(resource),
                                  open(resource, "rb"), "multipart/form-data")}

        self.set_upload_url()

    def set_upload_url(self):
        """Use to generate the full URL to POST to."""
        self._upload_url = "/".join([self.jss._url, self._url,
                                     self.resource_type, self.id_type,
                                     str(self._id)])

    #def save(self):
    #    """POST the object to the JSS."""
    #    try:
    #        response = requests.post(self._upload_url,
    #                                 auth=self.jss.session.auth,
    #                                 verify=self.jss.session.verify,
    #                                 files=self.resource)
    #    except JSSPostError as e:
    #        if e.status_code == 409:
    #            raise JSSPostError(e)
    #        else:
    #            raise JSSMethodNotAllowedError(self.__class__.__name__)

    #    if response.status_code == 201:
    #        if self.jss.verbose:
    #            print("POST: Success")
    #            print(response.text.encode("utf-8"))
    #    elif response.status_code >= 400:
    #        self.jss._error_handler(JSSPostError, response)

    def save(self):
        """POST the object to the JSS. WORKAROUND version."""
        try:
            # A regression introduced in JSS 9.64 prevents this from
            # working correctly. Until a solution is found, shell out
            # to curl.
            # This is defect D-008936.

            curl = ["/usr/bin/curl", "-kvu", "%s:%s" % self.jss.session.auth,
                    self._upload_url, "-F", "name=@%s" %
                    os.path.expanduser(self.resource_path), "-X", "POST"]
            response = subprocess.check_output(curl, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            # Handle problems with curl.
            raise JSSPostError("Curl subprocess error code: %s" % e.message)

        http_response_regex = ".*HTTP/1.1 ([0-9]{3}) ([a-zA-Z ]*)"
        found_patterns = re.findall(http_response_regex, response)
        if len(found_patterns) > 0:
            final_response = found_patterns[-1]
        else:
            raise JSSPostError("Unknown curl error.")
        if int(final_response[0]) >= 400:
            raise JSSPostError("Curl error: %s, %s" % final_response)
        elif int(final_response[0]) == 201:
            if self.jss.verbose:
                print("POST: Success")


class GSXConnection(JSSFlatObject):
    _url = "/gsxconnection"
    can_list = False
    can_post = False
    can_delete = False


class IBeacon(JSSContainerObject):
    _url = "/ibeacons"
    list_type = "ibeacon"


class JSSUser(JSSFlatObject):
    """JSSUser is deprecated."""
    _url = "/jssuser"
    can_list = False
    can_post = False
    can_put = False
    can_delete = False
    search_types = {}


class LDAPServer(JSSContainerObject):
    _url = "/ldapservers"

    def search_users(self, user):
        """Search for LDAP users.

        It is not entirely clear how the JSS determines the results-
        are regexes allowed, or globbing?

        Will raise a JSSGetError if no results are found.

        Returns an LDAPUsersResult object.

        """
        user_url = "%s/%s/%s" % (self.get_object_url(), "user", user)
        print(user_url)
        response = self.jss.get(user_url)
        return LDAPUsersResults(self.jss, response)

    def search_groups(self, group):
        """Search for LDAP groups.

        It is not entirely clear how the JSS determines the results-
        are regexes allowed, or globbing?

        Will raise a JSSGetError if no results are found.

        Returns an LDAPGroupsResult object.

        """
        group_url = "%s/%s/%s" % (self.get_object_url(), "group", group)
        response = self.jss.get(group_url)
        return LDAPGroupsResults(self.jss, response)

    def is_user_in_group(self, user, group):
        """Test for whether a user is in a group. Returns bool."""
        search_url = "%s/%s/%s/%s/%s" % (self.get_object_url(), "group", group,
                                         "user", user)
        response = self.jss.get(search_url)
        # Sanity check
        length = len(response)
        result = False
        if length  == 1:
            # User doesn't exist. Use default False value.
            pass
        elif length == 2:
            if response.findtext("ldap_user/username") == user:
                if response.findtext("ldap_user/is_member") == "Yes":
                    result = True
        elif len(response) >= 2:
            raise JSSGetError("Unexpected response.")
        return result

    # There is also the ability to test for whether multiple users are
    # members of an LDAP group, but you should just call
    # is_user_in_group over an enumerated list of users.

    @property
    def id(self):
        """Return object ID or None."""
        # LDAPServer's ID is in "connection"
        result = self.findtext("connection/id")
        return result

    @property
    def name(self):
        """Return object name or None."""
        # LDAPServer's name is in "connection"
        result = self.findtext("connection/name")
        return result


class LDAPUsersResults(JSSContainerObject):
    """Helper class for results of LDAPServer queries for users."""
    can_get = False
    can_post = False
    can_put = False
    can_delete = False


class LDAPGroupsResults(JSSContainerObject):
    """Helper class for results of LDAPServer queries for groups."""
    can_get = False
    can_post = False
    can_put = False
    can_delete = False


class LicensedSoftware(JSSContainerObject):
    _url = "/licensedsoftware"


class MacApplication(JSSContainerObject):
    _url = "/macapplications"
    list_type = "mac_application"


class ManagedPreferenceProfile(JSSContainerObject):
    _url = "/managedpreferenceprofiles"


class MobileDevice(JSSDeviceObject):
    """Mobile Device objects include a "match" search type which queries
    across multiple properties.

    """
    _url = "/mobiledevices"
    list_type = "mobile_device"
    search_types = {"name": "/name/", "serial_number": "/serialnumber/",
                    "udid": "/udid/", "macaddress": "/macadress/",
                    "match": "/match/"}

    @property
    def wifi_mac_address(self):
        """Return device's WIFI MAC address or None."""
        return self.findtext("general/wifi_mac_address")

    @property
    def bluetooth_mac_address(self):
        """Return device's Bluetooth MAC address or None."""
        return self.findtext("general/bluetooth_mac_address") or \
            self.findtext("general/mac_address")


class MobileDeviceApplication(JSSContainerObject):
    _url = "/mobiledeviceapplications"


class MobileDeviceCommand(JSSContainerObject):
    _url = "/mobiledevicecommands"
    can_put = False
    can_delete = False
    search_types = {"name": "/name/", "uuid": "/uuid/",
                    "command": "/command/"}
    # TODO: This object _can_ post, but it works a little differently
    can_post = False


class MobileDeviceConfigurationProfile(JSSContainerObject):
    _url = "/mobiledeviceconfigurationprofiles"


class MobileDeviceEnrollmentProfile(JSSContainerObject):
    _url = "/mobiledeviceenrollmentprofiles"
    search_types = {"name": "/name/", "invitation": "/invitation/"}


class MobileDeviceExtensionAttribute(JSSContainerObject):
    _url = "/mobiledeviceextensionattributes"


class MobileDeviceInvitation(JSSContainerObject):
    _url = "/mobiledeviceinvitations"
    can_put = False
    search_types = {"invitation": "/invitation/"}


class MobileDeviceGroup(JSSGroupObject):
    _url = "/mobiledevicegroups"
    list_type = "mobile_device_group"

    def add_mobile_device(self, device):
        """Add a mobile_device to the group."""
        super(MobileDeviceGroup, self).add_device(device, "mobile_devices")

    def remove_mobile_device(self, device):
        """Remove a mobile_device from the group."""
        super(MobileDeviceGroup, self).remove_object_from_list(
            device, "mobile_devices")


class MobileDeviceProvisioningProfile(JSSContainerObject):
    _url = "/mobiledeviceprovisioningprofiles"
    search_types = {"name": "/name/", "uuid": "/uuid/"}


class NetbootServer(JSSContainerObject):
    _url = "/netbootservers"


class NetworkSegment(JSSContainerObject):
    _url = "/networksegments"


class OSXConfigurationProfile(JSSContainerObject):
    _url = "/osxconfigurationprofiles"


class Package(JSSContainerObject):
    _url = "/packages"
    list_type = "package"

    def new(self, filename, **kwargs):
        name = ElementTree.SubElement(self, "name")
        name.text = filename
        category = ElementTree.SubElement(self, "category")
        category.text = kwargs.get("cat_name")
        fname = ElementTree.SubElement(self, "filename")
        fname.text = filename
        ElementTree.SubElement(self, "info")
        ElementTree.SubElement(self, "notes")
        priority = ElementTree.SubElement(self, "priority")
        priority.text = "10"
        reboot = ElementTree.SubElement(self, "reboot_required")
        reboot.text = "false"
        fut = ElementTree.SubElement(self, "fill_user_template")
        fut.text = "false"
        feu = ElementTree.SubElement(self, "fill_existing_users")
        feu.text = "false"
        boot_volume = ElementTree.SubElement(self, "boot_volume_required")
        boot_volume.text = "true"
        allow_uninstalled = ElementTree.SubElement(self, "allow_uninstalled")
        allow_uninstalled.text = "false"
        ElementTree.SubElement(self, "os_requirements")
        required_proc = ElementTree.SubElement(self, "required_processor")
        required_proc.text = "None"
        switch_w_package = ElementTree.SubElement(self, "switch_with_package")
        switch_w_package.text = "Do Not Install"
        install_if = ElementTree.SubElement(self,
                                            "install_if_reported_available")
        install_if.text = "false"
        reinstall_option = ElementTree.SubElement(self, "reinstall_option")
        reinstall_option.text = "Do Not Reinstall"
        ElementTree.SubElement(self, "triggering_files")
        send_notification = ElementTree.SubElement(self, "send_notification")
        send_notification.text = "false"

    def set_os_requirements(self, requirements):
        """Sets package OS Requirements. Pass in a string of comma
        seperated OS versions. A lowercase "x" is allowed as a wildcard,
        e.g. "10.9.x"

        """
        self.find("os_requirements").text = requirements

    def set_category(self, category):
        """Sets package category to "category", which can be a string of
        an existing category's name, or a Category object.

        """
        # For some reason, packages only have the category name, not the
        # ID.
        if isinstance(category, Category):
            name = category.name
        else:
            name = category
        self.find("category").text = name

    def save(self):
        """Save a new package to the JSS, or update an existing one."""
        # Jamf seems to have changed the way a missing category is
        # handled. If you try to update an existing policy with the data
        # returned from a GET on a policy that has no category, it will
        # fail. If we clear the category under those circumstances, it
        # will work.
        # See issue: D-008180
        category = self.find("category")
        if category.text == "No category assigned":
            self.set_category("")

        super(Package, self).save()


class Peripheral(JSSContainerObject):
    _url = "/peripherals"
    search_types = {}


class PeripheralType(JSSContainerObject):
    _url = "/peripheraltypes"
    search_types = {}


class Policy(JSSContainerObject):
    _url = "/policies"
    list_type = "policy"

    def new(self, name="Unknown", category=None):
        """Create a barebones policy.

        name:       Policy name
        category:   An instance of Category

        """
        # General
        self.general = ElementTree.SubElement(self, "general")
        self.name_element = ElementTree.SubElement(self.general, "name")
        self.name_element.text = name
        self.enabled = ElementTree.SubElement(self.general, "enabled")
        self.set_bool(self.enabled, True)
        self.frequency = ElementTree.SubElement(self.general, "frequency")
        self.frequency.text = "Once per computer"
        self.category = ElementTree.SubElement(self.general, "category")
        if category:
            # Without a category, the JSS will add an id of -1, with
            # name "Unknown". But... See D-008180
            self.category_name = ElementTree.SubElement(self.category, "name")
            self.category_name.text = category.name

        # Scope
        self.scope = ElementTree.SubElement(self, "scope")
        self.computers = ElementTree.SubElement(self.scope, "computers")
        self.computer_groups = ElementTree.SubElement(self.scope,
                                                      "computer_groups")
        self.buildings = ElementTree.SubElement(self.scope, "buldings")
        self.departments = ElementTree.SubElement(self.scope, "departments")
        self.exclusions = ElementTree.SubElement(self.scope, "exclusions")
        self.excluded_computers = ElementTree.SubElement(self.exclusions,
                                                         "computers")
        self.excluded_computer_groups = ElementTree.SubElement(
            self.exclusions, "computer_groups")
        self.excluded_buildings = ElementTree.SubElement(
            self.exclusions, "buildings")
        self.excluded_departments = ElementTree.SubElement(self.exclusions,
                                                           "departments")

        # Self Service
        self.self_service = ElementTree.SubElement(self, "self_service")
        self.use_for_self_service = ElementTree.SubElement(
            self.self_service, "use_for_self_service")
        self.set_bool(self.use_for_self_service, True)

        # Package Configuration
        self.pkg_config = ElementTree.SubElement(self, "package_configuration")
        self.pkgs = ElementTree.SubElement(self.pkg_config, "packages")

        # Maintenance
        self.maintenance = ElementTree.SubElement(self, "maintenance")
        self.recon = ElementTree.SubElement(self.maintenance, "recon")
        self.set_bool(self.recon, True)

    def add_object_to_scope(self, obj):
        """Add an object "obj" to the appropriate scope block."""
        if isinstance(obj, Computer):
            self.add_object_to_path(obj, "scope/computers")
        elif isinstance(obj, ComputerGroup):
            self.add_object_to_path(obj, "scope/computer_groups")
        elif isinstance(obj, Building):
            self.add_object_to_path(obj, "scope/buildings")
        elif isinstance(obj, Department):
            self.add_object_to_path(obj, "scope/departments")
        else:
            raise TypeError

    def clear_scope(self):
        """Clear all objects from the scope, including exclusions."""
        clear_list = ["computers", "computer_groups", "buildings",
                      "departments", "limit_to_users/user_groups",
                      "limitations/users", "limitations/user_groups",
                      "limitations/network_segments", "exclusions/computers",
                      "exclusions/computer_groups", "exclusions/buildings",
                      "exclusions/departments", "exclusions/users",
                      "exclusions/user_groups", "exclusions/network_segments"]
        for section in clear_list:
            self.clear_list("%s%s" % ("scope/", section))

    def add_object_to_exclusions(self, obj):
        """Add an object "obj" to the appropriate scope exclusions
        block.

        obj should be an instance of Computer, ComputerGroup, Building,
        or Department.

        """
        if isinstance(obj, Computer):
            self.add_object_to_path(obj, "scope/exclusions/computers")
        elif isinstance(obj, ComputerGroup):
            self.add_object_to_path(obj, "scope/exclusions/computer_groups")
        elif isinstance(obj, Building):
            self.add_object_to_path(obj, "scope/exclusions/buildings")
        elif isinstance(obj, Department):
            self.add_object_to_path(obj, "scope/exclusions/departments")
        else:
            raise TypeError

    def add_package(self, pkg):
        """Add a jss.Package object to the policy with
        action=install.

        """
        if isinstance(pkg, Package):
            package = self.add_object_to_path(
                pkg, "package_configuration/packages")
            action = ElementTree.SubElement(package, "action")
            action.text = "Install"

    def set_self_service(self, state=True):
        """Convenience setter for self_service."""
        self.set_bool(self.find("self_service/use_for_self_service"), state)

    def set_recon(self, state=True):
        """Convenience setter for recon."""
        self.set_bool(self.find("maintenance/recon"), state)

    def set_category(self, category):
        """Set the policy's category.

        category should be a category object.

        """
        pcategory = self.find("general/category")
        pcategory.clear()
        id_ = ElementTree.SubElement(pcategory, "id")
        id_.text = category.id
        name = ElementTree.SubElement(pcategory, "name")
        name.text = category.name

    def save(self):
        """Save a new policy to the JSS, or update an existing one."""
        # Jamf seems to have changed the way a missing category is
        # handled. If you try to update an existing policy with the data
        # returned from a GET on a policy that has no category, it will
        # fail. If we clear the category under those circumstances, it
        # will work.
        # See issue: D-008180
        category = self.find("general/category")
        if category.findtext("id") == "-1":
            category.remove(category.find("name"))
            category.remove(category.find("id"))

        super(Policy, self).save()


class Printer(JSSContainerObject):
    _url = "/printers"


class RestrictedSoftware(JSSContainerObject):
    _url = "/restrictedsoftware"


class RemovableMACAddress(JSSContainerObject):
    _url = "/removablemacaddresses"


class SavedSearch(JSSContainerObject):
    _url = "/savedsearches"
    can_put = False
    can_post = False
    can_delete = False


class Script(JSSContainerObject):
    _url = "/scripts"
    list_type = "script"


class Site(JSSContainerObject):
    _url = "/sites"
    list_type = "site"


class SoftwareUpdateServer(JSSContainerObject):
    _url = "/softwareupdateservers"


class SMTPServer(JSSFlatObject):
    _url = "/smtpserver"
    id_url = ""
    can_list = False
    can_post = False
    search_types = {}


class UserExtensionAttribute(JSSContainerObject):
    _url = "/userextensionattributes"


class User(JSSContainerObject):
    _url = "/users"


class UserGroup(JSSContainerObject):
    _url = "/usergroups"


class VPPAccount(JSSContainerObject):
    _url = "/vppaccounts"
    list_type = "vpp_account"


class SearchCriteria(ElementTree.Element):
    """Object for encapsulating a smart group search criteria."""
    list_type = "criterion"

    def __init__(self, name, priority, and_or, search_type, value):
        super(SearchCriteria, self).__init__(tag=self.list_type)
        crit_name = ElementTree.SubElement(self, "name")
        crit_name.text = name
        crit_priority = ElementTree.SubElement(self, "priority")
        crit_priority.text = str(priority)
        crit_and_or = ElementTree.SubElement(self, "and_or")
        crit_and_or.text = and_or
        crit_search_type = ElementTree.SubElement(self, "search_type")
        crit_search_type.text = search_type
        crit_value = ElementTree.SubElement(self, "value")
        crit_value.text = value

    def makeelement(self, tag, attrib):
        """Return an Element."""
        # We use ElementTree.SubElement() a lot. Unfortunately, it
        # relies on a super() call to its __class__.makeelement(), which
        # will fail due to the method resolution order / multiple
        # inheritance of our objects (they have an editor AND a template
        # or JSSObject parent class).
        # This handles that issue.
        return ElementTree.Element(tag, attrib)


class JSSListData(dict):
    """Holds information retrieved as part of a list operation."""
    def __init__(self, obj_class, d):
        self.obj_class = obj_class
        super(JSSListData, self).__init__(d)

    @property
    def id(self):
        return int(self["id"])

    @property
    def name(self):
        return self["name"]


class JSSObjectList(list):
    """A list style collection of JSSObjects.

    List operations retrieve only minimal information for most object
    types.  Further, we may want to know all Computer(s) to get their
    ID's, but that does not mean we want to do a full object search for
    each one. Thus, methods are provided to both retrieve individual
    members' full information, and to retrieve the full information for
    the entire list.

    """
    def __init__(self, factory, obj_class, objects):
        self.factory = factory
        self.obj_class = obj_class
        super(JSSObjectList, self).__init__(objects)

    def __repr__(self):
        """Make our data human readable.

        Note: Large lists of large objects may take a long time due to
        indenting!

        """
        delimeter = 50 * "-" + "\n"
        output_string = delimeter
        for obj in self:
            output_string += "List index: \t%s\n" % self.index(obj)
            for k, v in obj.items():
                output_string += "%s:\t\t%s\n" % (k, v)
            output_string += delimeter
        return output_string.encode("utf-8")

    def sort(self):
        """Sort list elements by ID."""
        super(JSSObjectList, self).sort(key=lambda k: k.id)

    def sort_by_name(self):
        """Sort list elements by name."""
        super(JSSObjectList, self).sort(key=lambda k: k.name)

    def retrieve(self, index):
        """Return a JSSObject for the JSSListData element at index."""
        return self.factory.get_object(self.obj_class, self[index].id)

    def retrieve_by_id(self, id_):
        """Return a JSSObject for the JSSListData element with ID
        id_.

        """
        list_index = [int(i) for i, j in enumerate(self) if j.id == id_]
        if len(list_index) == 1:
            list_index = list_index[0]
            return self.factory.get_object(self.obj_class, self[list_index].id)

    def retrieve_all(self, subset=None):
        """Return a list of all JSSListData elements as full JSSObjects.

        At least on my JSS, I end up with some harmless SSL errors,
        which are dealt with.

        Note: This can take a long time given a large number of objects,
        and depending on the size of each object.

        Args:
            subset:
                For objects which support it, a list of sub-tags to
                request, or an "&" delimited string, (e.g.
                "general&purchasing"). Default to None.
        """
        final_list = []
        for i in range(0, len(self)):
            result = self.factory.get_object(
                self.obj_class, int(self[i]["id"]), subset)
            final_list.append(result)

        return final_list
