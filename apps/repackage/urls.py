"""
repackage.urls
--------------
"""

from django.conf.urls.defaults import url, patterns

urlpatterns = patterns('repackage.views',

    url(r'^rebuild/$', 'rebuild', name='repackage_rebuild'),

)
