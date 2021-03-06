import os
import csv
import shutil
import time
import commonware
import tarfile
import markdown
import hashlib
import codecs

from copy import deepcopy

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from django.db.models.signals import (pre_save, post_delete, post_save,
                                      m2m_changed)
from django.db import models, transaction, IntegrityError
from django.utils import simplejson
from django.utils.html import escape
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.template.defaultfilters import slugify
from django.conf import settings
from django.utils.translation import ugettext as _


from cuddlefish.preflight import vk_to_jid, jid_to_programid, my_b32encode
from ecdsa import SigningKey, NIST256p
from elasticutils import es_required
from pyes import djangoutils
from pyes.exceptions import NotFoundException as PyesNotFoundException

from base.models import BaseModel
from jetpack.errors import (SelfDependencyException, FilenameExistException,
                            UpdateDeniedException, SingletonCopyException,
                            DependencyException, AttachmentWriteException)
from jetpack.managers import PackageManager
from utils import validator
from utils.exceptions import SimpleException
from utils.helpers import pathify, alphanum, alphanum_plus
from utils.os_utils import make_path
from xpi import xpi_utils

log = commonware.log.getLogger('f.jetpack')

def make_name(value=None):
    " wrap for slugify "
    return slugify(value)

def _get_full_name(full_name, username, type_id, i=0):
    """
    increment until there is no package with the full_name
    """
    new_full_name = full_name
    if i > 0:
        new_full_name = "%s (%d)" % (full_name, i)
    packages = Package.objects.filter(author__username=username,
                                      full_name=new_full_name,
                                      type=type_id)
    if packages.count() == 0:
        return new_full_name

    i = i + 1
    return _get_full_name(full_name, username, type_id, i)


PERMISSION_CHOICES = (
    (0, 'private'),
    (1, 'view'),
    (2, 'do not copy'),
    (3, 'edit')
)
TYPE_CHOICES = (
    ('l', 'Library'),
    ('a', 'Add-on')
)


class PackageRevision(BaseModel):
    """
    contains data which may be changed and rolled back
    """
    package = models.ForeignKey('Package', related_name='revisions')
    #: public version name
    #: this is a tag used to mark important revisions
    version_name = models.CharField(max_length=250, blank=True, null=True,
                                    default=settings.INITIAL_VERSION_NAME)
    #: name of the Package
    full_name = models.CharField(max_length=255, blank=True)
    #: name in revision is used for dependencies, and packages dir creation
    #: it should be also used in front-end
    name = models.CharField(max_length=250)
    #: this makes the revision unique across the same package/user
    revision_number = models.IntegerField(blank=True, default=0)
    #: commit message
    message = models.TextField(blank=True)
    #: autmagical message
    commit_message = models.TextField(blank=True, null=True)

    #: Libraries on which current package depends
    dependencies = models.ManyToManyField('self', blank=True, null=True,
                                            symmetrical=False)

    #: from which revision this mutation was originated
    origin = models.ForeignKey('PackageRevision', related_name='mutations',
                                blank=True, null=True)

    #: person who owns this revision
    author = models.ForeignKey(User, related_name='package_revisions')

    created_at = models.DateTimeField(auto_now_add=True)

    contributors = models.CharField(max_length=255, blank=True, default='')

    #: main for the Manifest
    module_main = models.CharField(max_length=100, blank=True, default='main')

    #: SDK which should be used to create the XPI
    sdk = models.ForeignKey('SDK', blank=True, null=True)

    class Meta:
        " PackageRevision ordering and uniqueness "
        ordering = ('-revision_number',)
        unique_together = ('package', 'author', 'revision_number')

    def __unicode__(self):
        version = 'v. %s ' % self.version_name if self.version_name else ''
        return '%s - %s %sr. %d by %s' % (
                            settings.PACKAGE_SINGULAR_NAMES[self.package.type],
                            self.full_name, version,
                            self.revision_number, self.author.get_profile()
                            )
    # NAME and FULL_NAME in Revision #############

    def set_full_name(self, value):
        """Sets the full name of the package

        :param: value (string) new full name of the package

        :raises: IntegrityError if Package with tht name already exists
        """
        if not validator.is_valid('alphanum_plus_space', value):
            raise ValidationError
        try:
            # in FlightDeck, libraries can have the same name,
            # by different authors
            Package.objects.get(author=revision.package.author,
                                name=make_name(package_full_name))
        except:
            self.full_name = value
            self.package.full_name = value
            self.add_commit_message("Package name changed to \"%s\"" %
                    self.full_name)
        else:
            raise IntegrityError

    def get_dir_name(self, packages_dir):
        return os.path.join(packages_dir, self.name + '-' + self.package.id_number)

    def make_dir(self, packages_dir):
        """
        create package directories inside packages
        return package directory name
        """
        package_dir = self.get_dir_name(packages_dir)
        if not os.path.isdir(package_dir):
            os.mkdir(package_dir)
        else:
            return False

        os.mkdir('%s/%s' % (package_dir, self.get_lib_dir()))
        if not os.path.isdir('%s/%s' % (package_dir, self.get_data_dir())):
            os.mkdir('%s/%s' % (package_dir, self.get_data_dir()))
        return package_dir

    def get_lib_dir(self):
        " returns the name of the lib directory in SDK default - packages "
        return self.package.lib_dir or settings.JETPACK_LIB_DIR

    def get_data_dir(self):
        " returns the name of the data directory in SDK default - data "
        # it stays as method as it could be saved in instance in the future
        # TODO: YAGNI!
        return settings.JETPACK_DATA_DIR

    def default_full_name(self):
        self.full_name = self.package.full_name

    def default_name(self):
        self.name = self.package.name

    def update_name(self, force=False):
        self.name = make_name(self.full_name)
        if self.pk and self.pk != self.package.latest:
            return
        if self.package.name != self.name:
            log.debug('Package name changed from (%s) to (%s)' % (
                self.package.name, self.name))
            self.package.name = self.name
        if force:
            super(Package, self).save()

    # URLS #############

    def get_absolute_url(self):
        " returns URL to view the package revision "
        if self.version_name:
            if self.package.version.revision_number == self.revision_number:
                return self.package.get_absolute_url()
            return reverse(
                'jp_%s_version_details' \
                % settings.PACKAGE_SINGULAR_NAMES[self.package.type],
                args=[self.package.id_number, self.version_name])
        return reverse(
            'jp_%s_revision_details' \
            % settings.PACKAGE_SINGULAR_NAMES[self.package.type],
            args=[self.package.id_number, self.revision_number])

    def get_save_url(self):
        " returns URL to save the package revision "
        return reverse(
            'jp_%s_revision_save' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_add_module_url(self):
        " returns URL to add module to the package revision "
        return reverse(
            'jp_%s_revision_add_module' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_rename_module_url(self):
        " returns URL to rename module in the package revision "
        return reverse(
            'jp_%s_revision_rename_module' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_remove_module_url(self):
        " returns URL to remove module from the package revision "
        return reverse(
            'jp_%s_revision_remove_module' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_upload_attachment_url(self):
        " returns URL to upload attachment to the package revision "
        return reverse(
            'jp_%s_revision_upload_attachment' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_add_attachment_url(self):
        " returns URL to add attachment to the package revision "
        return reverse('jp_revision_add_attachment', args=[self.pk])

    def get_rename_attachment_url(self):
        " returns URL to rename module in the package revision "
        return reverse(
            'jp_%s_revision_rename_attachment' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_remove_attachment_url(self):
        " returns URL to remove attachment from the package revision "
        return reverse(
            'jp_%s_revision_remove_attachment' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_assign_library_url(self):
        " returns url to assign library to the package revision "
        return reverse(
            'jp_%s_revision_assign_library' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_update_library_url(self):
        " returns url to update library to a specific version "
        return reverse(
            'jp_%s_revision_update_library' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_remove_library_url(self):
        " returns url to remove library from the package revision "
        return reverse(
            'jp_%s_revision_remove_library' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_test_xpi_url(self):
        " returns URL to test Add-on "
        if self.package.type != 'a':
            raise Exception('XPI might be created only from an Add-on')
        return reverse(
            'jp_addon_revision_test',
            args=[self.package.id_number, self.revision_number])

    def get_download_xpi_url(self):
        " returns URL to download Add-on's XPI "
        if self.package.type != 'a':
            raise Exception('XPI might be created only from an Add-on')
        return reverse(
            'jp_addon_revision_xpi',
            args=[self.package.id_number, self.revision_number])

    def get_copy_url(self):
        " returns URL to copy the package "
        return reverse(
            'jp_%s_revision_copy' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_switch_sdk_url(self):
        " returns URL to switch SDK on the package revision "
        return reverse(
            'jp_addon_switch_sdk_version',
            args=[self.package.id_number, self.revision_number])

    def get_add_folder_url(self):
        return reverse(
            'jp_%s_revision_add_folder' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_remove_folder_url(self):
        return reverse(
            'jp_%s_revision_remove_folder' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_latest_dependencies_url(self):
        return reverse(
            'jp_%s_check_latest_dependencies' % self.package.get_type_name(),
            args=[self.package.id_number, self.revision_number])

    def get_modules_list_url(self):
        return reverse('jp_revision_get_modules_list', args=[self.pk])

    def get_conflicting_modules_list_url(self):
        return reverse('jp_revision_get_conflicting_modules_list',
                args=[self.pk])

    ######################
    # Manifest

    def get_contributors_list(self):
        " returns a CSV build from the list of contributors "
        csv_r = csv.reader([self.contributors], skipinitialspace=True)
        for contributors in csv_r:
            return contributors

    def get_dependencies_list(self, sdk=None):
        " returns a list of dependencies names extended by default core "
        if self.package.is_addon() and self.sdk.kit_lib:
            deps = ['addon-kit']
        else:
            if sdk and sdk.kit_lib:
                deps = ['api-utils']
            else:
            # jetpack-core or api-utils
                deps = ['jetpack-core']
        deps.extend(["%s-%s" % (dep.name, dep.package.id_number) \
                     for dep in self.dependencies.all()])
        return deps

    def get_full_description(self):
        " return joined package and its revision description "
        description = self.package.description
        if self.message:
            description = "%s\n%s" % (description, self.message)
        return description

    def get_full_rendered_description(self):
        " return description prepared for rendering "
        return "<p>%s</p>" % self.get_full_description().replace("\n", "<br/>")

    def get_manifest(self, test_in_browser=False, sdk=None):
        " returns manifest dictionary "
        version = self.get_version_name()
        if test_in_browser:
            version = "%s - test" % version

        name = self.name
        if not self.package.is_addon():
            name = "%s-%s" % (name, self.package.id_number)

        manifest = {
            'fullName': self.full_name,
            'name': name,
            'description': self.get_full_description(),
            'author': self.package.author.get_profile().get_nickname(),
            'id': self.package.jid if self.package.is_addon() \
                    else self.package.id_number,
            'version': version,
            'dependencies': self.get_dependencies_list(sdk),
            'license': self.package.license,
            'url': str(self.package.url),
            'contributors': self.get_contributors_list(),
            'lib': self.get_lib_dir()
        }
        if self.package.is_addon():
            manifest['main'] = self.module_main

        return manifest

    def get_manifest_json(self, sdk=sdk, **kwargs):
        " returns manifest as JSOIN object "
        return simplejson.dumps(self.get_manifest(sdk=sdk, **kwargs))

    def get_main_module(self):
        " return executable Module for Add-ons "
        if self.package.type == 'l':
            return None

        # find main module
        main = self.modules.filter(filename=self.module_main)
        if not main:
            raise Exception(
                'Every Add-on needs to be linked with an executable Module')
        return main[0]

    def get_version_name(self):
        """Returns version name with revision number if needed."""

        return self.version_name \
                if self.version_name \
                else "%s.rev%s" % (
                    self.package.revisions.exclude(version_name=None)
                        .filter(revision_number__lt=self.revision_number)[0]
                            .version_name, self.revision_number)

    def get_version_name_only(self):
        """Returns version name."""

        return self.version_name \
                if self.version_name \
                else self.package.revisions.exclude(
                        version_name=None).filter(
                                revision_number__lt=self.revision_number
                        )[0].version_name

    def get_module_names(self):
        """Return all used module names
        """
        module_names = {
                self.name: [mod.filename for mod in self.modules.all()]}
        for dep in self.dependencies.all():
            module_names.update(dep.get_module_names())
        return module_names

    def get_conflicting_module_names(self):

        def _add_conflict(package_name, module_name):
            if not conflicts.get(package_name, False):
                conflicts[package_name] = []
            if module_name not in conflicts[package_name]:
                conflicts[package_name].append(module_name)

        conflicts = {}
        tuples = []
        # iterate over all packages imported
        for package_name, module_names in self.get_module_names().items():
            # iterate over all modules within the package
            for module_name in module_names:
                # get existing module names
                if tuples:
                    package, modules = zip(*tuples)
                else:
                    modules = []
                # check for conflict
                if module_name in modules:
                    _add_conflict(package_name, module_name)
                    # add other packages
                    # module name has to appear at least twice in conflicts
                    for tup in tuples:
                        if tup[1] == module_name:
                            _add_conflict(tup[0], tup[1])
                tuples.append((package_name, module_name))
        return conflicts


    ######################
    # revision save methods

    def add_commit_message(self, msg, force_save=False):
        """Collect messages for the revision
        """
        if not hasattr(self, '_commit_messages'):
            self._commit_messages = []

        if msg not in self._commit_messages:
            self._commit_messages.append(msg)
        if force_save:
            self.update_commit_message(True)

    def default_commit_message(self):
        """commit message is added for new revisions only
        """
        self.commit_message = self.build_commit_message()

    def build_commit_message(self):
        if hasattr(self, '_commit_messages') and self._commit_messages:
            return ', '.join(self._commit_messages)
        return ''

    def update_commit_message(self, force_save=False):
        self.commit_message = self.build_commit_message()
        if force_save:
            super(PackageRevision, self).save()

    def save(self, **kwargs):
        """
        overloading save is needed to prevent from updating the same revision
        use super(PackageRevision, self).save(**kwargs) if needed
        """
        if self.id:
            # create new revision
            return self.save_new_revision(**kwargs)
        return super(PackageRevision, self).save(**kwargs)

    def save_new_revision(self, package=None, **kwargs):
        " save self as new revision with link to the origin. "
        origin = deepcopy(self)
        if package:
            self.package = package
            self.author = package.author
        # reset instance - force saving a new one
        self.id = None
        self.version_name = None
        self.message = ''
        self.commit_message = ''
        self.origin = origin
        self.revision_number = self.get_next_revision_number()

        save_return = super(PackageRevision, self).save(**kwargs)
        # reassign all dependencies
        for dep in origin.dependencies.all():
            self.dependencies.add(dep)

        for dir in origin.folders.all():
            self.folders.add(dir)

        for mod in origin.modules.all():
            self.modules.add(mod)

        for att in origin.attachments.all():
            self.attachments.add(att)

        self.package.latest = self
        self.package.save()
        if package:
            self.set_version('copy')
            self.add_commit_message('package copied', force_save=True)
        return save_return

    def get_next_revision_number(self):
        """
        find latest revision_number for the self.package and self.user
        @return latest revisiion number or 1
        """
        revision_numbers = PackageRevision.objects.filter(
                                    author__username=self.author.username,
                                    package__id_number=self.package.id_number
                                ).order_by('-revision_number')
        return revision_numbers[0].revision_number + 1 \
                if revision_numbers else 1

    def set_version(self, version_name, current=True):
        """
        @param String version_name: name of the version
        @param Boolean current: should the version become a current one
        @returns result of save revision

        Set the version_name
        update the PackageRevision obeying the overload save
        Set current Package:version_name and Package:version if current
        """
        # check if there isn't a version with such a name
        if PackageRevision.objects.filter(package__pk=self.package.pk,
                version_name=version_name).count() > 0:
            # reset version_name
            version_name = ''
        self.add_commit_message('version changed')
        self.version_name = version_name
        if current and version_name:
            self.package.version_name = version_name
            self.package.version = self
            self.package.save()

        return super(PackageRevision, self).save()

    def validate_module_filename(self, filename):
        """
        returns False if the package revision contains a module with given
        filename
        """
        if self.modules.filter(filename=filename).count():
            return False
        return True

    def validate_attachment_filename(self, filename, ext):
        """
        returns False if the package revision contains a module with given
        filename
        """
        if self.attachments.filter(filename=filename, ext=ext).count():
            return False
        return True

    def validate_folder_name(self, foldername, root_dir):
        if self.folders.filter(name=foldername, root_dir=root_dir).count():
            return False
        return True

    def module_create(self, save=True, **kwargs):
        " create module and add to modules "
        # validate if given filename is valid
        if not self.validate_module_filename(kwargs['filename']):
            raise FilenameExistException(
                ('Sorry, there is already a module in your add-on'
                 'with the name "%s". Each module in your add-on'
                 'needs to have a unique name.') % kwargs['filename']
            )
        mod = Module.objects.create(**kwargs)
        self.module_add(mod, save=save)
        return mod

    def module_add(self, mod, save=True):
        " copy to new revision, add module "
        # validate if given filename is valid
        mod.clean()
        if not self.validate_module_filename(mod.filename):
            raise FilenameExistException(
                ('Sorry, there is already a module in your add-on '
                 'with the name "%s". Each module in your add-on '
                 'needs to have a unique name.') % mod.filename
            )
        self.add_commit_message(_('module (%s.js) added' % mod.filename))

        if save:
            self.save()
        return self.modules.add(mod)

    def module_remove(self, *mods):
        " copy to new revision, remove module(s) "
        self.add_commit_message(_('removing module(s)'))
        self.save()
        return self.modules.remove(*mods)

    def modules_remove_by_path(self, filenames):

        found_modules = []
        module_found = False
        empty_dirs_paths = []

        for filename in filenames:
            if filename[-1] == '/':
                empty_dirs_paths.append(filename[:-1])
                modules = self.modules.filter(filename__startswith=filename)
            else:
                modules = self.modules.filter(filename=filename)
            if modules.count() > 0:
                for mod in modules:
                    found_modules.append(mod)
                    module_found = True

        if not module_found:
            raise Module.DoesNotExist

        self.save()
        self.modules.remove(*found_modules)

        log.warning(self.folders.all())
        for path in empty_dirs_paths:
            folder = self.folders.get(root_dir='l', name=path)
            self.folders.remove(folder)
        return ([mod.filename for mod in found_modules], empty_dirs_paths)

    def folder_add(self, dir, save=True):
        " copy to new revision, add EmptyDir "
        errorMsg = ('Sorry, there is already a folder in your add-on '
                 'with the name "%s". Each folder in your add-on '
                 'needs to have a unique name.') % dir.name
        dir.clean()

        if not self.validate_folder_name(dir.name, dir.root_dir):
            raise FilenameExistException(errorMsg)

        # don't make EmptyDir for util/ if a file exists as util/example
        elif (dir.root_dir == 'l' and
            self.modules.filter(filename__startswith=dir.name).count()):
            raise FilenameExistException(errorMsg)
        elif (dir.root_dir == 'd' and
            self.attachments.filter(filename__startswith=dir.name).count()):
            raise FilenameExistException(errorMsg)

        self.add_commit_message('folder (%s) added' % dir.name)
        if save:
            self.save()
        return self.folders.add(dir)

    def folder_remove(self, dir):
        " copy to new revision, remove folder "
        self.add_commit_message('folder (%s) removed' % dir.name)
        self.save()
        return self.folders.remove(dir)

    def update(self, change, save=True):
        " to update a module, new package revision has to be created "
        if save:
            self.add_commit_message('data updated')
            self.save()
        return change.increment(self)

    def updates(self, changes, save=True):
        """Changes from the server."""
        if save:
            self.save()
        attachments_changed = {}
        names = []
        for change in changes:
            old_uid = change.pk
            ch = self.update(change, save=False)
            if isinstance(change, Attachment):
                attachments_changed[old_uid] = {'uid': ch.get_uid}
                names.append('%s.%s' % (change.filename, change.ext))
            else:
                names.append('%s.js' % change.filename)
        self.add_commit_message('content changed (%s)' % ', '.join(names))
        if save:
            self.update_commit_message(True)
        return attachments_changed

    def add_mods_and_atts_from_archive(self, packed, main, lib_dir, att_dir):
        """
        Read packed archive and search for modules and attachments
        """
        for path in packed.namelist():
            # add Modules
            if path.startswith(lib_dir):
                module_path = path.split('%s/' % lib_dir)[1]
                if module_path and not module_path.endswith('/'):
                    module_path = os.path.splitext(module_path)[0]
                    code = packed.read(path)
                    if module_path in [m.filename for m in self.modules.all()]:
                        mod = self.modules.get(filename=module_path)
                        mod.code = code
                        self.update(mod, save=False)
                    else:
                        self.module_create(
                                save=False,
                                filename=module_path,
                                author=self.author,
                                code=code)

            # add Attachments
            if path.startswith(att_dir):
                att_path = path.split('%s/' % att_dir)[1]
                if att_path and not att_path.endswith('/'):
                    code = packed.read(path)
                    filename, ext = os.path.splitext(att_path)
                    if ext.startswith('.'):
                        ext = ext.split('.')[1]

                    if (filename, ext) in [(a.filename, a.ext)
                            for a in self.attachments.all()]:
                        att = self.attachments.get(filename=filename, ext=ext)
                        att.data = code
                        self.update(att, save=False)
                    else:
                        att = self.attachment_create(
                                save=False,
                                filename=filename,
                                ext=ext,
                                path='temp',
                                author=self.author)
                        att.data = code
                        att.write()
                        self.attachments.add(att)

    @transaction.commit_on_success
    def attachment_create_by_filename(self, author, filename, content=''):
        """ find out the filename and ext and call attachment_create """
        filename, ext = os.path.splitext(filename)
        ext = ext.split('.')[1].lower() if ext else None

        kwargs = {
            'author': author,
            'filename': filename
        }

        if ext:
            kwargs['ext'] = ext

        attachment = self.attachment_create(**kwargs)

        # we must write data of some sort, in order to create the file on the disk
        # so at the least, write a blank string
        if not content:
            content = ''
        attachment.data = content
        attachment.write()
        return attachment

    def attachment_create(self, save=True, **kwargs):
        """ create attachment and add to attachments """
        att = Attachment(**kwargs)
        att.clean()

        if not self.validate_attachment_filename(att.filename, att.ext):
            raise FilenameExistException(
                ('Sorry, there is already an attachment in your add-on with '
                 'the name "%s.%s". Each attachment in your add-on needs to '
                 'have a unique name.') % (att.filename, att.ext)
            )

        att.save()
        self.attachment_add(att, save=save)
        return att

    def attachment_add(self, att, check=True, save=True):
        " copy to new revision, add attachment "
        # save as new version
        # validate if given filename is valid
        if (check and
            not self.validate_attachment_filename(att.filename, att.ext)):
            raise FilenameExistException(
                'Attachment with filename %s.%s already exists' % (
                    att.filename, att.ext)
            )

        self.add_commit_message('attachment (%s.%s) added' % (
            att.filename, att.ext))
        if save:
            self.save()
        return self.attachments.add(att)

    def attachment_remove(self, dep):
        " copy to new revision, remove attachment "
        # save as new version
        self.add_commit_message('attachment (%s.%s) removed' % (
            dep.filename, dep.ext))
        self.save()
        return self.attachments.remove(dep)

    def attachment_rmdir(self, path):
        """Remove whole directory of attachments
        """
        main_dir = path
        path = "%s/" % path
        attachments = self.attachments.filter(filename__startswith=path)
        dir_query = models.Q(name__startswith=path) | models.Q(name=main_dir)
        empty_dirs = self.folders.filter(dir_query)
        if not (attachments or empty_dirs):
            return None
        self.add_commit_message('folder (%s) removed' % path)
        self.save()
        removed_attachments = [att.pk for att in attachments]
        for att in attachments:
            self.attachments.remove(att)
        empty_dirs = self.folders.filter(dir_query)
        removed_empty_dirs = []
        i = 0
        while empty_dirs:
            for folder in empty_dirs:
                removed_empty_dirs.append(folder.name)
                self.folders.remove(folder)
            empty_dirs = self.folders.filter(dir_query)
            i += 1
        return self, removed_attachments, removed_empty_dirs

    def dependency_add(self, dep, save=True):
        """
        copy to new revision,
        add dependency (existing Library - PackageVersion)
        """
        # a PackageRevision has to depend on the LibraryRevision only
        if not dep.package.is_library():
            raise TypeError('Dependency has to be a Library')

        # a LibraryRevision can't depend on another LibraryRevision
        # linked with the same Library
        if dep.package.id_number == self.package.id_number:
            raise SelfDependencyException(
                'A Library can not depend on itself!')

        # dependency have to be unique in the PackageRevision
        # currently, the SDK can't compile with libraries with same "name"
        self.compare_dependency_conflicts(dep)
        self.add_commit_message('dependency (%s) added' % dep.name)
        if save:
            # save as new version
            self.save()
        return self.dependencies.add(dep)

    def compare_dependency_conflicts(self, dep, as_upgrade=False):
        """
        check if adding a dependency will cause a naming conflict as per the
        SDK 1.0. raises a DependencyException.

        :param: as_upgrade (Boolean) - if the passed `dep` should be compared
            for doing an upgade.

        Example:
            self = 'A' -> 'B' -> 'C'
            dep = 'D' -> 'E' -> 'F' -> 'C'
            raises DependencyException for 'C'.
        """
        def check_conflicts_if_added(existing, adding, first_level=False):
            """
            :param: first_level (Boolean) - conflicts are more strict at the
                first level
            """

            is_self = existing == self
            is_dep = adding == dep

            # as an upgrade, don't compare the upgraded revision to the old
            # revision of the same package
            if as_upgrade and first_level and existing.package_id == dep.package_id:
                return

            # same id is fine, we will only include same id
            # once when building the xpi.
            same_name = existing.package.name == adding.package.name
            same_revision = existing.id == adding.id

            if (same_name and (not same_revision or first_level)):
                raise DependencyException(
                    'Your %s already depends on a library named "%s"' % (
                        self.package.get_type_name(),
                        adding.name))
            for lib in existing.dependencies.all():
                check_conflicts_if_added(lib, adding, is_self and is_dep)


        def check_adding_all_dependencies(existing, adding):
            check_conflicts_if_added(existing, adding)

            for lib in adding.dependencies.all():
                check_adding_all_dependencies(existing, lib)

        if not as_upgrade and self.dependencies.filter(
            package__name=dep.package.name).count():
            raise DependencyException(
                    'Your %s already depends on a library with that name' % (
                        self.package.get_type_name(),))
        check_adding_all_dependencies(self, dep)

    def dependency_update(self, dep, save=True):
        " create new version with dependency version updated "
        try:
            old_version = self.dependencies.get(package=dep.package_id)
        except PackageRevision.DoesNotExist:
            raise DependencyException('This %s does not depend on "%s".'
                        % (self.package.get_type_name(), dep.full_name))

        if old_version == dep:
            raise DependencyException('"%s" is already up-to-date.'
                                      % dep.full_name)

        else:
            self.compare_dependency_conflicts(dep, as_upgrade=True)
            self.add_commit_message(
                    'dependency (%s) updated' % dep.name)
            if save:
                self.save()
            self.dependencies.remove(old_version)
            return self.dependencies.add(dep)

    def dependency_remove(self, dep):
        " copy to new revision, remove dependency "
        if self.dependencies.filter(pk=dep.pk).count() > 0:
            self.add_commit_message(
                    'dependency (%s) removed' % dep.name)
            # save as new version
            self.save()
            return self.dependencies.remove(dep)
        raise DependencyException(
            'There is no such library in this %s' \
            % self.package.get_type_name())

    def dependency_remove_by_id_number(self, id_number):
        " find dependency by its id_number call dependency_remove "
        for dep in self.dependencies.all():
            if dep.package.id_number == id_number:
                self.dependency_remove(dep)
                return True
        raise DependencyException(
            'There is no such library in this %s' \
            % self.package.get_type_name())

    def get_outdated_dependency_versions(self):
        " check all dependencies for a newer version "
        out_of_date = []
        for current_revision in self.dependencies.select_related('package'):
            latest_revision = current_revision.package.latest
            if current_revision != latest_revision:
                try:
                    self.compare_dependency_conflicts(latest_revision, as_upgrade=True)
                except DependencyException:
                    pass # dont offer the update, it has conflicts
                else:
                    out_of_date.append(latest_revision)
        return out_of_date

    def get_dependencies_list_json(self):
        " returns dependencies list as JSON object "
        d_list = [{
                'full_name': escape(d.full_name),
                'id_number': d.package.id_number,
                'view_url': d.get_absolute_url()
                } for d in self.dependencies.all()
            ] if self.dependencies.count() > 0 else []
        return simplejson.dumps(d_list)

    def get_modules_list_json(self):
        " returns modules list as JSON object "
        m_list = [{
                'filename': escape(m.filename),
                'author': escape(m.author.username),
                'executable': self.module_main == m.filename,
                'get_url': reverse('jp_module', args=[m.pk])
                } for m in self.modules.all()
            ] if self.modules.count() > 0 else []
        return simplejson.dumps(m_list)

    def get_attachments_list_json(self):
        " returns attachments list as JSON object "
        a_list = [{
                'uid': a.get_uid,
                'filename': escape(a.filename),
                'author': escape(a.author.username),
                'type': escape(a.ext),
                'get_url': reverse('jp_attachment', args=[a.get_uid])
                } for a in self.attachments.all()
            ] if self.attachments.count() > 0 else []
        return simplejson.dumps(a_list)

    def get_folders_list_json(self):
        " returns empty folders list as JSON object "
        f_list = [{
                'name': escape(f.name),
                'author': escape(f.author.username),
                'root_dir': escape(f.root_dir),
                } for f in self.folders.all()
            ] if self.folders.count() > 0 else []
        return simplejson.dumps(f_list)

    def get_modules_tree(self):
        " returns modules list as JSON object "
        return [{
                'path': m.filename,
                'get_url': reverse('jp_get_module', args=[
                    self.package.id_number,
                    self.revision_number,
                    m.filename])
                } for m in self.modules.all()
            ] if self.modules.count() > 0 else []

    def get_attachments_tree(self):
        " returns modules list as JSON object "
        return [{
                'path': a.filename,
                'get_url': a.get_display_url()
                } for a in self.attachments.all()
            ] if self.attachments.count() > 0 else []

    def get_dependencies_tree(self):
        " returns libraries "
        _lib_dict = lambda lib: {'path': lib.full_name,
                                 'url': lib.get_absolute_url()}

        libs = [_lib_dict(self.get_sdk_revision())] \
                if self.get_sdk_revision() else []
        if self.dependencies.count() > 0:
            libs.extend([_lib_dict(lib) for lib in self.dependencies.all()])
        return libs

    def get_sdk_name(self):
        " returns the name of the directory to which SDK should be copied "
        return '%s-%s-%s' % (self.sdk.version,
                             self.package.id_number, self.revision_number)

    def get_sdk_dir(self, hashtag):
        " returns the path to the directory where the SDK should be copied "
        return os.path.join(
                settings.SDKDIR_PREFIX,
                "%s-%s" % (hashtag, self.get_sdk_name()))

    def get_sdk_revision(self):
        " return core_lib, addon_kit or None "
        if not self.sdk:
            return None

        return self.sdk.kit_lib if self.sdk.kit_lib else self.sdk.core_lib

    def build_xpi(self, modules=[], attachments=[], hashtag=None, rapid=False):
        """
        prepare and build XPI for test only (unsaved modules)

        :param modules: list of modules from editor - potentially unsaved
        :param rapid: if True - do not use celery to produce xpi
        :rtype: dict containing load xpi information if rapid else AsyncResult

        This method is called from cellery task
        """
        if self.package.type == 'l':
            log.error("Attempt to build xpi (%s), but package is not an "
                      "add-on. Expected (l) but got (%s)." % (
                          self.package.type, self.get_version_name()))
            raise Exception("Only add-ons may build an xpi.")

        if not hashtag:
            log.error("Attempt to build add-on (%s) but it's missing a "
                      "hashtag.  Failing." % self.get_version_name())
            raise IntegrityError("Hashtag is required to create an xpi.")

        # XXX: this should be a tempfile directory
        sdk_dir = self.get_sdk_dir(hashtag)
        sdk_source = self.sdk.get_source_dir()

        # This SDK is always different! - working on unsaved data
        if os.path.isdir(sdk_dir):
            log.info("Attempt to build add-on (%s) using hashtag (%s) but the"
                     "file already exists.  Removing existing file. (%s)" %
                     (self.get_version_name(), hashtag, sdk_dir))
            shutil.rmtree(sdk_dir)

        xpi_utils.sdk_copy(sdk_source, sdk_dir)
        self.export_keys(sdk_dir)

        packages_dir = os.path.join(sdk_dir, 'packages')
        package_dir = self.make_dir(packages_dir)
        self.export_manifest(package_dir)

        # instead of export modules
        lib_dir = os.path.join(package_dir, self.get_lib_dir())
        for mod in self.modules.all():
            mod_edited = False
            for e_mod in modules:
                if e_mod.pk == mod.pk:
                    mod_edited = True
                    e_mod.export_code(lib_dir)
            if not mod_edited:
                mod.export_code(lib_dir)

        data_dir = os.path.join(package_dir, self.get_data_dir())
        for att in self.attachments.all():
            att_edited = False
            for e_att in attachments:
                if e_att.pk == att.pk:
                    att_edited = True
                    e_att.export_code(data_dir)
            if not att_edited:
                att.export_file(data_dir)
        #self.export_attachments(
        #    '%s/%s' % (package_dir, self.get_data_dir()))
        self.export_dependencies(packages_dir, sdk=self.sdk)
        args = [sdk_dir, self.get_dir_name(packages_dir),
                self.name, hashtag]
        return xpi_utils.build(*args)

    def export_keys(self, sdk_dir):
        """Export private and public keys to file."""
        keydir = os.path.join(sdk_dir, settings.KEYDIR)
        if not os.path.isdir(keydir):
            os.mkdir(keydir)

        keyfile = os.path.join(keydir, self.package.jid)
        with codecs.open(keyfile, mode='w', encoding='utf-8') as f:
            f.write('private-key:%s\n' % self.package.private_key)
            f.write('public-key:%s' % self.package.public_key)

    def export_manifest(self, package_dir, sdk=None):
        """Creates a file with an Add-on's manifest."""
        manifest_file = "%s/package.json" % package_dir
        with codecs.open(manifest_file, mode='w', encoding='utf-8') as f:
            f.write(self.get_manifest_json(sdk=sdk))

    def export_modules(self, lib_dir):
        """Creates a module file for each module."""
        for mod in self.modules.all():
            mod.export_code(lib_dir)

    def export_attachments(self, data_dir):
        """Creates an attachment file for each attachment."""
        for att in self.attachments.all():
            att.export_file(data_dir)

    def export_dependencies(self, packages_dir, sdk=None):
        """Creates dependency package directory for each dependency."""
        for lib in self.dependencies.all():
            lib.export_files_with_dependencies(packages_dir, sdk=sdk)

    def export_files(self, packages_dir, sdk=None):
        """Calls all export functions - creates all packages files."""
        package_dir = self.make_dir(packages_dir)
        if not package_dir:
            return
        self.export_manifest(package_dir, sdk=sdk)
        self.export_modules(
            os.path.join(package_dir, self.get_lib_dir()))
        self.export_attachments(
            os.path.join(package_dir, self.get_data_dir()))

    def export_files_with_dependencies(self, packages_dir, sdk=None):
        """Export dependency packages."""
        self.export_files(packages_dir, sdk=sdk)
        self.export_dependencies(packages_dir, sdk=sdk)

    @property
    def is_latest(self):
        return self.pk == self.package.latest.pk


class Package(BaseModel):
    """
    Holds the meta data shared across all PackageRevisions
    """
    #: identification,
    #: it can be the same as database id, but if we want to copy the database
    #: some day or change to a document-oriented database it would be bad
    #: to have this relied on any database model
    id_number = models.CharField(max_length=255, unique=True, blank=True)

    #: name of the Package
    full_name = models.CharField(max_length=255, blank=True)
    #: made from the full_name, used to create Package directory for export
    name = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)

    #: type - determining ability to specific options
    type = models.CharField(max_length=30, choices=TYPE_CHOICES)

    #: author is the first person who created the Package
    author = models.ForeignKey(User, related_name='packages_originated')

    #: is the Package visible for public?
    public_permission = models.IntegerField(
                                    choices=PERMISSION_CHOICES,
                                    default=1, blank=True)

    #: url for the Manifest
    url = models.URLField(verify_exists=False, blank=True, default='')

    #: license on which this package is released to the public
    license = models.CharField(max_length=255, blank=True, default='')

    #: where to export modules
    lib_dir = models.CharField(max_length=100, blank=True, null=True)

    #: this is set in the PackageRevision.set_version
    version_name = models.CharField(max_length=250, blank=True, null=True,
                                    default=settings.INITIAL_VERSION_NAME)

    #: Revision which is setting the version name
    version = models.ForeignKey('PackageRevision', blank=True, null=True,
                                related_name='+')

    #: latest saved PackageRevision
    latest = models.ForeignKey('PackageRevision', blank=True, null=True,
                               related_name='+')

    # signing an add-on
    private_key = models.TextField(blank=True, null=True)
    public_key = models.TextField(blank=True, null=True)
    jid = models.CharField(max_length=255, blank=True, null=True)
    program_id = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_update = models.DateTimeField(auto_now=True)

    # active do not show the package to others
    active = models.BooleanField(default=True, blank=True)
    # deleted is the limbo state
    deleted = models.BooleanField(default=False, blank=True)

    class Meta:
        " Set the ordering of objects "
        ordering = ('-last_update', '-created_at')
        unique_together = ('author', 'name')

    objects = PackageManager()

    ##################
    # Methods

    def save(self, **kwargs):
        try:
            super(Package, self).save(**kwargs)
        except IntegrityError, err:
            if Package.objects.filter(id_number=self.id_number).exclude(pk=self.pk):
                self.id_number = _get_next_id_number()
                self.save(**kwargs)

    def __unicode__(self):
        return '%s v. %s by %s' % (self.full_name, self.version_name,
                                   self.author)

    def get_absolute_url(self):
        " returns the URL View Source "
        return reverse('jp_%s_details' % self.get_type_name(),
                        args=[self.id_number])

    def get_latest_url(self):
        " returns the URL to view the latest saved Revision "
        return reverse('jp_%s_latest' % self.get_type_name(),
                        args=[self.id_number])

    def get_latest_revision_number_url(self):
        " returns url to get the latest revision number "
        return reverse('jp_get_latest_revision_number', args=[self.id_number])

    def get_disable_url(self):
        " returns URL to the disable package functionality "
        return reverse('jp_package_disable',
                        args=[self.id_number])

    def get_activate_url(self):
        " returns URL to activate disabled package "
        return reverse('jp_package_activate',
                        args=[self.id_number])

    def get_delete_url(self):
        " returns URL to delete package "
        return reverse('jp_package_delete',
                        args=[self.id_number])

    def is_addon(self):
        " returns Boolean: True if this package an Add-on "
        return self.type == 'a'

    def is_library(self):
        " returns Boolean: True if this package a Library "
        return self.type == 'l'

    def is_core(self):
        """
        returns Boolean: True if this is a SDK Core Library
        Used to block copying the package
        """
        return str(self.id_number) == str(settings.MINIMUM_PACKAGE_ID)

    def is_singleton(self):
        """
        Blocks copying the package
        """
        # Core lib is a singleton
        return self.is_core()

    def get_type_name(self):
        " return name of the type (add-on / library) "
        return settings.PACKAGE_SINGULAR_NAMES[self.type]

    def default_id_number(self):
        self.id_number = _get_next_id_number()

    def default_full_name(self):
        self.set_full_name()

    def default_name(self):
        self.name = make_name(self.full_name)


    def set_full_name(self):
        """
        setting automated full name of the Package item
        add incrementing number in brackets if author already has
        a package with default name
        called from signals
        """
        if self.full_name:
            return

        username = self.author.username
        if self.author.get_profile():
            username = self.author.get_profile().nickname or username

        name = username + settings.DEFAULT_PACKAGE_SUFFIX.get(self.type, '')
        self.full_name = _get_full_name(name, self.author.username, self.type)

    def generate_key(self):
        """
        create keypair, program_id and jid
        """

        signingkey = SigningKey.generate(curve=NIST256p)
        sk_text = "private-jid0-%s" % my_b32encode(signingkey.to_string())
        verifyingkey = signingkey.get_verifying_key()
        vk_text = "public-jid0-%s" % my_b32encode(verifyingkey.to_string())
        self.jid = vk_to_jid(verifyingkey)
        self.program_id = jid_to_programid(self.jid)
        self.private_key = sk_text
        self.public_key = vk_text

    def get_copied_full_name(self, basic_name=None, iteration=1):
        """
        Add "Copy of" before the full name if package is copied
        """
        full_name = self.full_name if not basic_name else basic_name
        if '(copy ' in full_name:
            full_name = full_name.split('(copy')[0]
        new_name = '%s (copy %d)' % (full_name, iteration)
        try:
            Package.objects.get(name=make_name(new_name))
        except ObjectDoesNotExist:
            return new_name
        return self.get_copied_full_name(
                basic_name=full_name, iteration=iteration+1)

    def copy(self, author):
        """
        create copy of the package
        """
        if self.is_singleton():
            raise SingletonCopyException("This is a singleton")
        new_p = Package(
            full_name=self.get_copied_full_name(),
            description=self.description,
            type=self.type,
            author=author,
            public_permission=self.public_permission,
            url=self.url,
            license=self.license,
            lib_dir=self.lib_dir
        )
        new_p.save()
        # Saving the track of forks
        new_p.latest.origin = self.latest
        super(PackageRevision, new_p.latest).save()
        return new_p

    def disable(self):
        """Set active tp False
        """
        self.active = False
        self.save()

    def delete(self):
        """Remove from the system if possible, otherwise mark as deleted
        Unhook from copies if needed and perform database delete
        """
        for rev_mutation in PackageRevision.objects.filter(
                origin__package=self):
            rev_mutation.origin = None
            # save without creating a new revision
            super(PackageRevision, rev_mutation).save()

        if not self.is_addon() and \
                PackageRevision.dependencies.through.objects.filter(
                        to_packagerevision__package=self):
            self.deleted = True
            log.info("Package (%s) marked as deleted" % self)
            return self.save()
        log.info("Package (%s) deleted" % self)
        return super(Package, self).delete()

    def create_revision_from_xpi(self, packed, manifest, author, jid,
            new_revision=False):
        """
        Create new package revision by reading XPI

        Args:
            packed (ZipFile): XPI
            manifest (dict): parsed package.json
            jid (String): jid name from XPI
            author (auth.User): owner of PackageRevision
            new_revision (Boolean): should new revision be created?

        Returns:
            PackageRevision object
        """

        revision = self.latest
        if 'contributors' in manifest:
            revision.contributors = manifest['contributors']

        main = manifest['main'] if 'main' in manifest else 'main'
        lib_dir = 'resources/%s-%s-%s' % (jid.lower(), manifest['name'],
                manifest['lib'] if 'lib' in manifest else 'lib')
        att_dir = 'resources/%s-%s-%s' % (
                jid.lower(), manifest['name'], 'data')

        revision.add_mods_and_atts_from_archive(packed, main, lib_dir, att_dir)

        if new_revision:
            revision.save()
        else:
            super(PackageRevision, revision).save()

        revision.set_version(manifest['version'])
        return revision

    def create_revision_from_archive(self, packed, manifest, author,
            new_revision=False):
        """
        Create new package revision vy reading the archive.

        Args:
            packed (ZipFile): archive containing Revision data
            manifest (dict): parsed package.json
            author (auth.User): owner of PackageRevision
            new_revision (Boolean): should new revision be created?

        Returns:
            PackageRevision object
        """

        revision = self.latest
        if 'contributors' in manifest:
            revision.contributors = manifest['contributors']

        main = manifest['main'] if 'main' in manifest else 'main'
        lib_dir = manifest['lib'] if 'lib' in manifest else 'lib'
        att_dir = 'data'

        revision.add_mods_and_atts_from_archive(packed, main, lib_dir, att_dir)

        if new_revision:
            revision.save()
        else:
            super(PackageRevision, revision).save()

        revision.set_version(manifest['version'])
        return revision

    def clean(self):
        self.full_name = alphanum_plus(self.full_name)
        if self.description:
            self.description = alphanum_plus(self.description)
        if self.version_name:
            self.version_name = alphanum_plus(self.version_name)

    @es_required
    def refresh_index(self, es, bulk=False):
        if not self.active:  # Don't index active things, and remove them.
            return self.remove_from_index(bulk=bulk)

        data = djangoutils.get_values(self)
        try:
            if self.latest:
                deps = self.latest.dependencies.all()
                data['dependencies'] = [dep.package.id for dep in deps]
        except PackageRevision.DoesNotExist:
            pass

        try:
            es.index(data, settings.ES_INDEX, self.get_type_name(), self.id,
                 bulk=bulk)
        except Exception, e:
            log.error("ElasticSearch errored for addon (%s): %s" % (self, e))
        else:
            log.debug('Package %d added to search index.' % self.id)

    def get_author_nickname(self):
        return self.author.get_profile().get_nickname()

    def get_author_profile_url(self):
        " returns URL to the view with author's profile "
        return reverse('person_public_profile', args=[self.get_author_nickname()])

    @es_required
    def remove_from_index(self, es, bulk=False):
        try:
            es.delete(settings.ES_INDEX, self.get_type_name(), self.id,
                 bulk=bulk)
        except PyesNotFoundException:
            pass
        except Exception, e:
            log.error("ElasticSearch error removing addon (%s): %s" %
                      (self, e))
        else:
            log.debug('Package %d removed from search index.' % self.id)


class Module(BaseModel):
    """
    Code used by Package.
    It's assigned to PackageRevision.

    The only way to 'change' the module is to assign it to
    a different PackageRevision
    """
    revisions = models.ManyToManyField(PackageRevision,
                                       related_name='modules', blank=True)
    # name of the Module - it will be used as javascript file name
    filename = models.CharField(max_length=255)
    # Code of the module
    code = models.TextField(blank=True)
    # user who has written current revision of the module
    author = models.ForeignKey(User, related_name='module_revisions')

    class Meta:
        " ordering for Module model "
        ordering = ('filename',)

    def __unicode__(self):
        return '%s by %s (%s)' % (self.get_filename(),
                                  self.author, self.get_package_fullName())

    def get_package(self):
        " returns first package to which the module is assigned "
        # TODO: Check if this method is used
        try:
            return self.revisions.all()[0].package
        except Exception:
            return None

    def get_package_fullName(self):
        " returns full name of the package to which the module is assigned "
        # TODO: Check if this method is used
        package = self.get_package()
        return package.full_name if package else ''

    def get_path(self):
        """
        Returns the path of directories that would be created from the
        filename.
        """
        parts = self.filename.split('/')[0:-1]
        return ('/'.join(parts)) if parts else None

    def get_filename(self):
        " returns the filename with extension (adds .js)"
        return "%s.js" % self.filename

    def save(self, *args, **kwargs):
        " overloaded to prevent from updating an existing module "
        if self.id:
            raise UpdateDeniedException(
                'Module can not be updated in the same row')
        return super(Module, self).save(*args, **kwargs)

    def export_code(self, lib_dir):
        """Creates a file containing the module."""

        path = os.path.join(lib_dir, self.get_filename())
        make_path(os.path.dirname(os.path.abspath(path)))
        with codecs.open(path, mode='w', encoding='utf-8') as f:
            f.write(self.code)

    def get_json(self):
        return simplejson.dumps({
            'filename': self.filename,
            'code': self.code,
            'author': self.author.username})

    def increment(self, revision):
        revision.modules.remove(self)
        self.pk = None
        self.save()
        revision.modules.add(self)
        return self

    def clean(self):
        first_period = self.filename.find('.')
        if first_period > -1:
            self.filename = self.filename[:first_period]

    def can_view(self, viewer=None):
        can_view_q = models.Q(package__active=True)
        if viewer and viewer.is_authenticated():
            can_view_q |= models.Q(package__author=viewer)

        return self.revisions.filter(can_view_q).count() > 0


class Attachment(BaseModel):
    """
    File (image, css, etc.) updated by the author of the PackageRevision
    When exported should be placed in a special directory - usually "data"
    """

    revisions = models.ManyToManyField(PackageRevision,
                                    related_name='attachments', blank=True)
    # filename of the attachment
    filename = models.CharField(max_length=255)

    # extension name
    ext = models.CharField(max_length=10, blank=True, default='js')

    # access to the file within upload/ directory
    path = models.CharField(max_length=255)

    # user who has uploaded the file
    author = models.ForeignKey(User, related_name='attachments')
    # mime will help with displaying the attachment
    mimetype = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        " attachment ordering "
        ordering = ('filename', 'id')

    @property
    def get_uid(self):
        """A uid that contains, filename and extension and is suitable
        for use in css selectors (eg: no spaces)."""
        return str(int(self.pk))

    @property
    def is_editable(self):
        return self.ext in ["html", "css", "js", "txt"]

    def get_filename(self):
        " returns human readable filename with extension "
        name = self.filename
        if self.ext:
            name = "%s.%s" % (name, self.ext)
        return name

    def get_path(self):
        """
        Returns the path of directories that would be created from the
        filename.
        """
        parts = self.filename.split('/')[0:-1]
        return ('/'.join(parts)) if parts else None

    def get_display_url(self):
        """Returns URL to display the attachment."""
        return reverse('jp_attachment', args=[self.get_uid])

    def default_path(self):
        self.create_path()

    def create_path(self):
        filename = hashlib.md5(self.filename + self.ext).hexdigest()

        args = (self.pk or 0, filename, )
        self.path = os.path.join(time.strftime('%Y/%m/%d'), '%s-%s' % args)

    def get_file_path(self):
        if self.path:
            return os.path.join(settings.UPLOAD_DIR, self.path)
        raise ValueError("self.path not set.")

    def read(self):
        """Reads the file, if it doesn't exist return empty."""
        if self.path and os.path.exists(self.get_file_path()):
            kwargs = {'mode': 'rb'}
            if self.is_editable:
                kwargs['encoding'] = 'utf-8'
                kwargs['mode'] = 'r'

            f = codecs.open(self.get_file_path(), **kwargs)
            content = f.read()
            f.close()
            return content
        return ""

    def changed(self):
        return self.read() != self.data

    def write(self):
        """Writes the file."""

        data = self.data if hasattr(self, 'data') else self.read()
        self.create_path()
        self.save()

        directory = os.path.dirname(self.get_file_path())

        if not os.path.exists(directory):
            os.makedirs(directory)

        kwargs = {'mode': 'wb'}
        if self.is_editable:
            kwargs['encoding'] = 'utf-8'
            kwargs['mode'] = 'w'

        try:
            with codecs.open(self.get_file_path(), **kwargs) as f:
                f.write(data)
        except UnicodeDecodeError:
            log.error('Attachment write failure: %s' % self.pk)
            raise AttachmentWriteException('Attachment failed to save properly')


    def export_code(self, static_dir):
        " creates a file containing the module "
        if not hasattr(self, 'code'):
            return self.export_file(static_dir)
        path = os.path.join(static_dir, '%s.%s' % (self.filename, self.ext))
        make_path(os.path.dirname(os.path.abspath(path)))
        with codecs.open(path, mode='w', encoding='utf-8') as f:
            f.write(self.code)

    def export_file(self, static_dir):
        " copies from uploads to the package's data directory "
        path = os.path.join(static_dir, '%s.%s' % (self.filename, self.ext))
        make_path(os.path.dirname(os.path.abspath(path)))
        shutil.copy(os.path.join(settings.UPLOAD_DIR, self.path),
                    path)

    def increment(self, revision):
        # revision is already incremented
        # attachment's filename might have changed, but we haven't changed
        # uid yet, so use that
        query = revision.attachments.filter(pk=self.pk)
        if query.count() > 1:
            log.warning(
                "Fixing revision by removing all duplicate attachments")
        for old in query:
            revision.attachments.remove(old)
        self.pk = None
        self.save()
        self.write()
        revision.attachments.add(self)
        return self

    def clean(self):
        self.filename = pathify(self.filename)
        if self.ext:
            self.ext = alphanum(self.ext)

    def can_view(self, viewer=None):
        can_view_q = models.Q(package__active=True)
        if viewer and viewer.is_authenticated():
            can_view_q |= models.Q(package__author=viewer)

        return self.revisions.filter(can_view_q).count() > 0


class EmptyDir(BaseModel):
    revisions = models.ManyToManyField(PackageRevision,
                                       related_name='folders', blank=True)
    name = models.CharField(max_length=255)
    author = models.ForeignKey(User, related_name='folders')

    ROOT_DIR_CHOICES = (
        ('l', settings.JETPACK_LIB_DIR),
        ('d', settings.JETPACK_DATA_DIR),
    )
    root_dir = models.CharField(max_length=10, choices=ROOT_DIR_CHOICES)

    def __unicode__(self):
        return '%s/' % self.name

    def clean(self):
        self.name = pathify(self.name).replace('.', '')


class SDK(BaseModel):
    """
    Jetpack SDK representation in database

    Add-ons have to depend on an SDK, by default on the latest one.
    """
    version = models.CharField(max_length=10, unique=True)

    # It has to be accompanied with a core library
    # needs to exist before SDK is created
    core_lib = models.OneToOneField(PackageRevision,
            related_name="parent_sdk_core+")
    kit_lib = models.OneToOneField(PackageRevision,
            related_name="parent_sdk_kit+", blank=True, null=True)
    #core_name = models.CharField(max_length=100, default='jetpack-core')
    #core_fullname = models.CharField(max_length=100, default='Jetpack Core')
    #kit_name = models.CharField(max_length=100, default='addon-kit')
    #kit_fullname = models.CharField(max_length=100, default='Addon Kit')

    # placement in the filesystem
    dir = models.CharField(max_length=255, unique=True)

    class Meta:
        """Ordering of SDK instances."""
        ordering = ["-id"]

    def __unicode__(self):
        return self.version

    def get_source_dir(self):
        return os.path.join(settings.SDK_SOURCE_DIR, self.dir)

    def is_deprecated(self):
        # TODO: This should be in the settings file as a config var (or in the
        # db and editable on the web)
        return self.version < '1.0b1'



def _get_next_id_number():
    """
    get the highest id number and increment it
    """
    all_packages_ids = [int(x.id_number) for x in Package.objects.all()]
    all_packages_ids.sort()
    return str(all_packages_ids[-1] + 1) \
            if all_packages_ids else str(settings.MINIMUM_PACKAGE_ID)


# Catching Signals


def make_keypair_on_create(instance, **kwargs):
    " creates public and private keys for JID "
    if kwargs.get('raw', False) or instance.id or not instance.is_addon():
        return
    instance.generate_key()
pre_save.connect(make_keypair_on_create, sender=Package)

def index_package(instance, **kwargs):
    from search.tasks import index_all
    index_all.delay([instance.id])

post_save.connect(index_package, sender=Package)

unindex_package = lambda instance, **kwargs: instance.remove_from_index()
post_delete.connect(unindex_package, sender=Package)


def index_package_m2m(instance, action, **kwargs):
    if action in ("post_add", "post_remove"):
        from search.tasks import index_all
        index_all.delay([instance.package.id])
m2m_changed.connect(index_package_m2m,
                    sender=PackageRevision.dependencies.through)


def save_first_revision(instance, **kwargs):
    """
    every Package has at least one PackageRevision - it's created here
    if Package is an Add-on it will create a Module as well.
    """
    if kwargs.get('raw', False) or not kwargs.get('created', False):
        return

    revision = PackageRevision(
        package=instance,
        author=instance.author)

    if instance.is_addon():
        sdks = SDK.objects.all()
        if len(sdks):
            revision.sdk = sdks[0]
    revision.save()
    instance.version = revision
    instance.latest = revision
    if instance.is_addon():
        first_module_name = revision.module_main
        first_module_code = """// This is an active module of the %s Add-on
exports.main = function() {};"""
    else:
        first_module_name = 'index'
        first_module_code = "// This is first module of the %s Library"
    mod = Module.objects.create(
        filename=first_module_name,
        author=instance.author,
        code=first_module_code % instance.full_name
    )
    revision.modules.add(mod)
    instance.save()
post_save.connect(save_first_revision, sender=Package)


def manage_empty_lib_dirs(instance, action, **kwargs):
    """
    create EmptyDirs when all modules in a "dir" are deleted,
    and remove EmptyDirs when any modules are added into the "dir"
    """
    if not (isinstance(instance, PackageRevision)
            and action in ('post_add', 'post_remove')):
        return

    pk_set = kwargs.get('pk_set', [])

    if action == 'post_add':
        for pk in pk_set:
            mod = Module.objects.get(pk=pk)
            dirname = mod.get_path()
            if not dirname:
                continue
            for d in instance.folders.filter(name=dirname, root_dir='l'):
                instance.folders.remove(d)

    elif action == 'post_remove':
        for pk in pk_set:
            mod = Module.objects.get(pk=pk)
            dirname = mod.get_path()
            if not dirname:
                continue

            if not instance.modules.filter(
                    filename__startswith=dirname).count():
                options = {'revisions__package': instance.package_id,
                           'name': dirname, 'root_dir': 'l'}
                try:
                    emptydir = EmptyDir.objects.get(**options)
                except EmptyDir.MultipleObjectsReturned:
                    emptydir = EmptyDir.objects.filter(**options)[0]
                except EmptyDir.DoesNotExist:
                    emptydir = EmptyDir(name=dirname, root_dir='l',
                                        author=instance.author)
                    emptydir.save()

                instance.folders.add(emptydir)
m2m_changed.connect(manage_empty_lib_dirs, sender=Module.revisions.through)


def manage_empty_data_dirs(instance, action, **kwargs):
    """
    create EmptyDirs when all modules in a "dir" are deleted,
    and remove EmptyDirs when any modules are added into the "dir"
    """
    if not (isinstance(instance, PackageRevision)
            and action in ('post_add', 'post_remove')):
        return

    pk_set = kwargs.get('pk_set', [])

    if action == 'post_add':
        for pk in pk_set:
            att = Attachment.objects.get(pk=pk)
            dirname = att.get_path()
            if not dirname:
                continue
            for d in instance.folders.filter(name=dirname, root_dir='d'):
                instance.folders.remove(d)

    elif action == 'post_remove':
        for pk in pk_set:
            att = Attachment.objects.get(pk=pk)
            dirname = att.get_path()
            if not dirname:
                continue

            if not instance.attachments.filter(
                    filename__startswith=dirname).count():
                options = {'revisions__package': instance.package_id,
                           'name': dirname, 'root_dir': 'd'}
                try:
                    emptydir = EmptyDir.objects.get(**options)
                except EmptyDir.MultipleObjectsReturned:
                    emptydir = EmptyDir.objects.filter(**options)[0]
                except EmptyDir.DoesNotExist:
                    emptydir = EmptyDir(name=dirname, root_dir='d',
                                        author=instance.author)
                    emptydir.save()

                instance.folders.add(emptydir)
m2m_changed.connect(manage_empty_data_dirs,
                    sender=Attachment.revisions.through)
