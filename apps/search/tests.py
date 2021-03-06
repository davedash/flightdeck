from django.conf import settings
from django.contrib.auth.models import User

import test_utils
from elasticutils import get_es
from nose import SkipTest
from nose.tools import eq_
from pyes import StringQuery, FieldQuery, FieldParameter

from jetpack.models import Package


def create_addon(name):
    return create_package(name, type='a')


def create_library(name):
    return create_package(name, type='l')


def create_package(name, type):
    u = User.objects.get(username='john')
    return Package.objects.create(full_name=name, author=u, type=type)


# TODO: liberate - this can go in either test_utils or a separate library.
class ESTestCase(test_utils.TestCase):
    """
    ESTestCase turns ElasticSearch on, shuts it down at the end of the tests.
    """
    @classmethod
    def setUpClass(cls):
        if not hasattr(settings, 'ES_BIN'):
            raise SkipTest
        cls.old_ES_DISABLED = settings.ES_DISABLED
        settings.__dict__['ES_DISABLED'] = False

        cls.es = get_es()
        cls.es.delete_index_if_exists(settings.ES_INDEX)

    def tearDown(self):
        self.es.delete_index_if_exists(settings.ES_INDEX)

    @classmethod
    def tearDownClass(cls):
        settings.__dict__['ES_DISABLED'] = cls.old_ES_DISABLED


class TestSearch(ESTestCase):
    fixtures = ('mozilla_user', 'users', 'core_sdk')

    def test_index(self):
        """Create an add-on and verify that we can find it in elasticsearch."""
        a = create_addon('zool')
        es = self.es
        es.refresh()
        r = es.search(query=StringQuery('zool'))
        eq_(r['hits']['total'], 1)
        eq_(r['hits']['hits'][0]['_source']['name'], a.name)
        return a

    def test_index_delete(self):
        """Create an add-on then delete it, verify it's not in the index."""
        a = self.test_index()
        a.delete()
        es = self.es
        es.refresh()
        r = es.search(query=StringQuery('zool'))
        eq_(r['hits']['total'], 0, "We shouldn't get any hits.")

    def test_index_dependencies(self):
        """
        Test that we are indexing the dependent ids.

        In this example we'll make a libraries bar and barf.
        Which add-on foo is dependent on....

        WILL IT INDEX?

        """
        bar = create_library('bar')
        barf = create_library('barf')
        addon = create_addon('foo')
        addon.latest.dependency_add(bar.latest)
        addon.latest.dependency_add(barf.latest)
        es = self.es
        es.refresh()

        for lib in (bar, barf):
            r = es.search(query=FieldQuery(FieldParameter('dependencies',
                                                          lib.id)))
            eq_(r['hits']['total'], 1)
            eq_(r['hits']['hits'][0]['_source']['name'], addon.name)
        return (addon, bar, barf)

    def test_index_dependencies_remove(self):
        (addon, bar, barf) = self.test_index_dependencies()
        addon.latest.dependency_remove(bar.latest)
        addon.latest.dependency_remove(barf.latest)
        es = self.es
        es.refresh()

        for lib in (bar, barf):
            r = es.search(query=FieldQuery(FieldParameter('dependencies',
                                                          lib.id)))
            eq_(r['hits']['total'], 0)
