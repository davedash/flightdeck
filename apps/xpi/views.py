import os
import commonware.log
import codecs

from django.views.static import serve
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.conf import settings

from base.shortcuts import get_object_with_related_or_404
from utils import validator
from utils.helpers import get_random_string
from xpi import xpi_utils

from jetpack import tasks
from jetpack.models import PackageRevision, SDK


log = commonware.log.getLogger('f.xpi')

@csrf_exempt
@require_POST
def prepare_test(r, id_number, revision_number=None):
    """
    Test XPI from data saved in the database
    """
    revision = get_object_with_related_or_404(PackageRevision,
                        package__id_number=id_number, package__type='a',
                        revision_number=revision_number)
    hashtag = r.POST.get('hashtag')
    if not hashtag:
        log.warning('[security] No hashtag provided')
        return HttpResponseForbidden('{"error": "No hashtag"}')
    if not validator.is_valid('alphanum', hashtag):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    # prepare codes to be sent to the task
    mod_codes = {}
    att_codes = {}
    if r.POST.get('live_data_testing', False):
        for mod in revision.modules.all():
            if r.POST.get(mod.filename, False):
                code = r.POST.get(mod.filename, '')
                if mod.code != code:
                    mod_codes[str(mod.pk)] = code
        for att in revision.attachments.all():
            if r.POST.get(str(att.pk), False):
                code = r.POST.get(str(att.pk))
                att_codes[str(att.pk)] = code
    tasks.xpi_build_from_model.delay(revision.pk,
            mod_codes=mod_codes, att_codes=att_codes, hashtag=hashtag)
    return HttpResponse('{"delayed": true}')

@never_cache
def get_test(r, hashtag):
    """
    return XPI file for testing
    """
    if not validator.is_valid('alphanum', hashtag):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    path = os.path.join(settings.XPI_TARGETDIR, '%s.xpi' % hashtag)
    mimetype = 'text/plain; charset=x-user-defined'
    try:
        xpi = codecs.open(path, mode='rb').read()
    except Exception, err:
        log.debug('Add-on not yet created: %s' % str(err))
        return HttpResponse('')
    log.info('Downloading Add-on: %s' % hashtag)
    return HttpResponse(xpi, mimetype=mimetype)

@csrf_exempt
@require_POST
def prepare_download(r, id_number, revision_number=None):
    """
    Prepare download XPI.  This package is built asynchronously and we assume
    it works. It will be downloaded in ``get_download``
    """
    revision = get_object_with_related_or_404(PackageRevision,
                        package__id_number=id_number, package__type='a',
                        revision_number=revision_number)
    hashtag = r.POST.get('hashtag')
    if not hashtag:
        return HttpResponseForbidden('Add-on Builder has been updated!'
                'We have updated this part of the application. Please '
                'empty your cache and reload to get changes.')
    if not validator.is_valid('alphanum', hashtag):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    tasks.xpi_build_from_model.delay(revision.pk, hashtag=hashtag)
    return HttpResponse('{"delayed": true}')


@never_cache
def check_download(r, hashtag):
    """Check if XPI file is prepared."""
    if not validator.is_valid('alphanum', hashtag):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    path = os.path.join(settings.XPI_TARGETDIR, '%s.xpi' % hashtag)
    # Check file if it exists
    if os.path.isfile(path):
        return HttpResponse('{"ready": true}')
    return HttpResponse('{"ready": false}')


@never_cache
def get_download(r, hashtag, filename):
    """
    Download XPI (it has to be ready)
    """
    if not validator.is_valid('alphanum', hashtag):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    path = os.path.join(settings.XPI_TARGETDIR, '%s.xpi' % hashtag)
    log.info('Downloading %s.xpi from %s' % (filename, path))
    response = serve(r, path, '/', show_indexes=False)
    response['Content-Disposition'] = ('attachment; '
            'filename="%s.xpi"' % filename)
    return response


@never_cache
def clean(r, path):
    " remove whole temporary SDK on request "
    # Validate sdk_name
    if not validator.is_valid('alphanum', path):
        log.warning('[security] Wrong hashtag provided')
        return HttpResponseForbidden("{'error': 'Wrong hashtag'}")
    xpi_utils.remove(os.path.join(settings.XPI_TARGETDIR, '%s.xpi' % path))
    return HttpResponse('{"success": true}', mimetype='application/json')



@never_cache
def repackage(r, amo_id, amo_file, target_version=None, sdk_dir=None):
    """Pull amo_id/amo_file.xpi, schedule xpi creation, return hashtag
    """
    # validate entries
    # prepare data
    hashtag = get_random_string(10)
    sdk = SDK.objects.all()[0]
    # if (when?) choosing sdk_dir will be possible
    # sdk = SDK.objects.get(dir=sdk_dir) if sdk_dir else SDK.objects.all()[0]
    sdk_source_dir = sdk.get_source_dir()
    # extract packages
    tasks.repackage.delay(
            amo_id, amo_file, sdk_source_dir, hashtag, target_version)
    # call build xpi task
    # respond with a hashtag which will identify downloadable xpi
    # URL to check if XPI is ready:
    # /xpi/check_download/{hashtag}/
    # URL to download:
    # /xpi/download/{hashtag}/{desired_filename}/
    return HttpResponse('{"hashtag": "%s"}' % hashtag,
            mimetype='application/json')
