# coding=utf-8
import os
import shutil
import simplejson
import commonware

from mock import Mock
from nose.tools import eq_
from utils.test import TestCase

from django.contrib.auth.models import User
from django.conf import settings

from jetpack.models import Module, Package, PackageRevision, SDK
from xpi import xpi_utils
from base.templatetags.base_helpers import hashtag

log = commonware.log.getLogger('f.tests')


class XPIBuildTest(TestCase):

    fixtures = ['mozilla', 'core_sdk', 'users', 'packages']

    def setUp(self):
        self.hashtag = hashtag()
        self.author = User.objects.get(username='john')
        self.addon = Package.objects.get(name='test-addon',
                                         author__username='john')
        self.library = Package.objects.get(name='test-library')
        self.addonrev = self.addon.latest
        self.librev = self.library.latest
        mod = Module.objects.create(
            filename='test_module',
            code='// test module',
            author=self.author
        )
        self.librev.module_add(mod)
        self.SDKDIR = self.addon.latest.get_sdk_dir(self.hashtag)
        self.attachment_file_name = os.path.join(
                settings.UPLOAD_DIR, 'test_filename.txt')
        handle = open(self.attachment_file_name, 'w')
        handle.write('.')
        handle.close()
        # link core to the latest SDK
        self.createCore()
        settings.XPI_AMO_PREFIX = "file://%s" % os.path.join(
                settings.ROOT, 'apps/xpi/tests/sample_addons/')

    def tearDown(self):
        self.deleteCore()
        if os.path.exists(self.SDKDIR):
            shutil.rmtree(self.SDKDIR)
        if os.path.exists(self.attachment_file_name):
            os.remove(self.attachment_file_name)

    def makeSDKDir(self):
        if self.SDKDIR and os.path.isdir(self.SDKDIR):
            shutil.rmtree(self.SDKDIR)
        os.mkdir(self.SDKDIR)
        os.mkdir('%s/packages' % self.SDKDIR)

    def test_package_dir_generation(self):
        " test if all package dirs are created properly "
        self.makeSDKDir()
        package_dir = self.library.latest.make_dir('%s/packages' % self.SDKDIR)
        self.failUnless(os.path.isdir(package_dir))
        self.failUnless(os.path.isdir(
            '%s/%s' % (package_dir, self.library.latest.get_lib_dir())))

    def test_save_modules(self):
        " test if module is saved "
        self.makeSDKDir()
        package_dir = self.library.latest.make_dir('%s/packages' % self.SDKDIR)
        self.librev.export_modules(
            '%s/%s' % (package_dir, self.library.latest.get_lib_dir()))

        self.failUnless(os.path.isfile('%s/%s/%s.js' % (
                            package_dir,
                            self.library.latest.get_lib_dir(),
                            'test_module')))

    def test_manifest_file_creation(self):
        " test if manifest is created properly "
        self.makeSDKDir()
        package_dir = self.library.latest.make_dir('%s/packages' % self.SDKDIR)
        self.librev.export_manifest(package_dir)
        self.failUnless(os.path.isfile('%s/package.json' % package_dir))
        handle = open('%s/package.json' % package_dir)
        manifest_json = handle.read()
        manifest = simplejson.loads(manifest_json)
        self.assertEqual(manifest, self.librev.get_manifest())

    def test_minimal_lib_export(self):
        " test if all the files are in place "
        self.makeSDKDir()
        self.librev.export_files_with_dependencies('%s/packages' % self.SDKDIR)
        package_dir = self.librev.get_dir_name('%s/packages' % self.SDKDIR)
        self.failUnless(os.path.isdir(package_dir))
        self.failUnless(os.path.isdir(
            '%s/%s' % (package_dir, self.library.latest.get_lib_dir())))
        self.failUnless(os.path.isfile('%s/package.json' % package_dir))
        self.failUnless(os.path.isfile('%s/%s/%s.js' % (
                            package_dir,
                            self.library.latest.get_lib_dir(),
                            'test_module')))

    def test_addon_export_with_dependency(self):
        " test if lib and main.js are properly exported "
        self.makeSDKDir()
        addon_dir = self.addon.latest.get_dir_name('%s/packages' % self.SDKDIR)
        lib_dir = self.library.latest.get_dir_name('%s/packages' % self.SDKDIR)

        self.addonrev.dependency_add(self.librev)
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        self.failUnless(os.path.isdir(
            '%s/%s' % (addon_dir, self.addon.latest.get_lib_dir())))
        self.failUnless(os.path.isdir(
            '%s/%s' % (lib_dir, self.library.latest.get_lib_dir())))
        self.failUnless(os.path.isfile(
            '%s/%s/%s.js' % (
                addon_dir,
                self.addon.latest.get_lib_dir(),
                self.addonrev.module_main)))

    def test_addon_export_with_attachment(self):
        """Test if attachment file is copied."""
        self.makeSDKDir()
        # create attachment in upload dir
        handle = open(self.attachment_file_name, 'w')
        handle.write('unit test file')
        handle.close()
        attachment = self.addonrev.attachment_create(
            filename='test_filename.txt',
            author=self.author
        )
        attachment.create_path()
        attachment.data = ''
        attachment.write()
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        self.failUnless(os.path.isfile(self.attachment_file_name))

    def test_copying_sdk(self):
        xpi_utils.sdk_copy(self.addonrev.sdk.get_source_dir(), self.SDKDIR)
        self.failUnless(os.path.isdir(self.SDKDIR))

    def test_minimal_xpi_creation(self):
        " xpi build from an addon straight after creation "
        xpi_utils.sdk_copy(self.addonrev.sdk.get_source_dir(), self.SDKDIR)
        self.addonrev.export_keys(self.SDKDIR)
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        err = xpi_utils.build(
                self.SDKDIR,
                self.addon.latest.get_dir_name('%s/packages' % self.SDKDIR),
                self.addon.name, self.hashtag)
        # assert no error output
        assert not err[1]
        # assert xpi was created
        self.failUnless(os.path.isfile(
            "%s.xpi" % os.path.join(settings.XPI_TARGETDIR, self.hashtag)))

    def test_addon_with_other_modules(self):
        " addon has now more modules "
        self.addonrev.module_create(
            filename='test_filename',
            author=self.author
        )
        xpi_utils.sdk_copy(self.addonrev.sdk.get_source_dir(), self.SDKDIR)
        self.addonrev.export_keys(self.SDKDIR)
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        err = xpi_utils.build(
                self.SDKDIR,
                self.addon.latest.get_dir_name('%s/packages' % self.SDKDIR),
                self.addon.name, self.hashtag)
        # assert no error output
        assert not err[1]
        # assert xpi was created
        self.failUnless(os.path.isfile(
            "%s.xpi" % os.path.join(settings.XPI_TARGETDIR, self.hashtag)))

    def test_xpi_with_empty_dependency(self):
        " empty lib is created "
        lib = Package.objects.create(
            full_name='Test Library XPI',
            author=self.author,
            type='l'
        )
        librev = PackageRevision.objects.filter(
            package__id_number=lib.id_number)[0]
        self.addonrev.dependency_add(librev)
        xpi_utils.sdk_copy(self.addonrev.sdk.get_source_dir(), self.SDKDIR)
        self.addonrev.export_keys(self.SDKDIR)
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        err = xpi_utils.build(
                self.SDKDIR,
                self.addon.latest.get_dir_name('%s/packages' % self.SDKDIR),
                self.addon.name, self.hashtag)
        # assert no error output
        assert not err[1]
        # assert xpi was created
        self.failUnless(os.path.isfile(
            "%s.xpi" % os.path.join(settings.XPI_TARGETDIR, self.hashtag)))

    def test_xpi_with_dependency(self):
        " addon has one dependency with a file "
        self.addonrev.dependency_add(self.librev)
        xpi_utils.sdk_copy(self.addonrev.sdk.get_source_dir(), self.SDKDIR)
        self.addonrev.export_keys(self.SDKDIR)
        self.addonrev.export_files_with_dependencies(
            '%s/packages' % self.SDKDIR)
        err = xpi_utils.build(
                self.SDKDIR,
                self.addon.latest.get_dir_name('%s/packages' % self.SDKDIR),
                self.addon.name, self.hashtag)
        # assert no error output
        assert not err[1]
        # assert xpi was created
        self.failUnless(os.path.isfile(
            "%s.xpi" % os.path.join(settings.XPI_TARGETDIR, self.hashtag)))

    def test_module_with_utf(self):

        mod = Module.objects.create(
            filename='test_utf',
            code='// ą',
            author=self.author
        )
        self.library.latest.module_add(mod)
        self.makeSDKDir()
        package_dir = self.library.latest.make_dir('%s/packages' % self.SDKDIR)
        self.librev.export_modules(
            '%s/%s' % (package_dir, self.library.latest.get_lib_dir()))

        self.failUnless(os.path.isfile('%s/%s/%s.js' % (
                            package_dir,
                            self.library.latest.get_lib_dir(),
                            'test_module')))

    def test_package_included_multiple_times(self):
        """ If separate dependencies require the same library, it shouldn't error """
        pack = Package.objects.create(type='l', author=self.author)
        packrev = pack.latest
        self.librev.dependency_add(packrev)
        self.addonrev.dependency_add(packrev)
        self.addonrev.dependency_add(self.librev)

        self.addonrev.build_xpi(hashtag=self.hashtag, rapid=True)

