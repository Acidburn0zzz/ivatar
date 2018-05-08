from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _

from . gravatar import get_photo as get_gravatar_photo

from openid.store.interface import OpenIDStore
from openid.store import nonce as oidnonce
from openid.association import Association as OIDAssociation

import datetime
def utcnow():
    return datetime.datetime.utcnow

MAX_LENGTH_EMAIL = 254  # http://stackoverflow.com/questions/386294
MAX_LENGTH_URL = 255  # MySQL can't handle more than that (LP: 1018682)

class BaseAccountModel(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.deletion.CASCADE,
    )
    ip_address = models.GenericIPAddressField(unpack_ipv4=True)
    add_date = models.DateTimeField(default=utcnow)

    class Meta:
        abstract = True


class Photo(BaseAccountModel):
    ip_address = models.GenericIPAddressField(unpack_ipv4=True)
    data = models.BinaryField()
    format = models.CharField(max_length=3)

    class Meta:
        verbose_name = _('photo')
        verbose_name_plural = _('photos')


class ConfirmedEmailManager(models.Manager):
    def create_confirmed_email(self, user, email_address, is_logged_in):
        confirmed = ConfirmedEmail()
        confirmed.user = user
        confirmed.ip_address = '0.0.0.0'
        confirmed.email = email_address
        confirmed.save()

        external_photos = []
        if is_logged_in:
            gravatar = get_gravatar_photo(confirmed.email)
            if gravatar:
                external_photos.append(gravatar)

        return external_photos


class ConfirmedEmail(BaseAccountModel):
    email = models.EmailField(unique=True, max_length=MAX_LENGTH_EMAIL)
    photo = models.ForeignKey(
        Photo,
        related_name='emails',
        blank=True,
        null=True,
        on_delete=models.deletion.CASCADE,
    )
    objects = ConfirmedEmailManager()

    class Meta:
        verbose_name = _('confirmed email')
        verbose_name_plural = _('confirmed emails')


class UnconfirmedEmail(BaseAccountModel):
    email = models.EmailField(max_length=MAX_LENGTH_EMAIL)
    verification_key = models.CharField(max_length=64)

    class Meta:
        verbose_name = _('unconfirmed_email')
        verbose_name_plural = _('unconfirmed_emails')

    def save(self, *args, **kwargs):
        hash_object = hashlib.new('sha256')
        hash_object.update(urandom(1024) + self.user.username)
        self.verification_key = hash_object.hexdigest()

        super(UnconfirmedEmail, self).save(*args, **kwargs)


class UnconfirmedOpenId(BaseAccountModel):
    openid = models.URLField(unique=False, max_length=MAX_LENGTH_URL)

    class Meta:
        verbose_name = _('unconfirmed OpenID')
        verbose_name_plural = ('unconfirmed_OpenIDs')


class ConfirmedOpenId(BaseAccountModel):
    openid = models.URLField(unique=True, max_length=MAX_LENGTH_URL)
    photo = models.ForeignKey(
        Photo,
        related_name='openids',
        blank=True,
        null=True,
        on_delete = models.deletion.CASCADE,
    )

    class Meta:
        verbose_name = _('confirmed OpenID')
        verbose_name_plural = _('confirmed OpenIDs')

# Classes related to the OpenID Store (from https://github.com/edx/django-openid-auth/)
class OpenIDNonce(models.Model):
    server_url = models.CharField(max_length=255)
    timestamp = models.IntegerField()
    salt = models.CharField(max_length=128)

class OpenIDAssociation(models.Model):
    server_url = models.TextField(max_length=2047)
    handle = models.CharField(max_length=255)
    secret = models.TextField(max_length=255)  # stored base64 encoded
    issued = models.IntegerField()
    lifetime = models.IntegerField()
    assoc_type = models.TextField(max_length=64)


class DjangoOpenIDStore(OpenIDStore):
    '''
    The Python openid library needs an OpenIDStore subclass to persist data
    related to OpenID authentications. This one uses our Django models.
    '''

    def storeAssociation(self, server_url, association):
        # pylint: disable=unexpected-keyword-arg
        assoc = OpenIDAssociation(
            server_url=server_url,
            handle=association.handle,
            secret=base64.encodestring(association.secret),
            issued=association.issued,
            lifetime=association.issued,
            assoc_type=association.assoc_type)
        assoc.save()

    def getAssociation(self, server_url, handle=None):
        assocs = []
        if handle is not None:
            assocs = OpenIDAssociation.objects.filter(
                server_url=server_url, handle=handle)
        else:
            assocs = OpenIDAssociation.objects.filter(server_url=server_url)
        if not assocs:
            return None
        associations = []
        for assoc in assocs:
            if type(assoc.secret) is str:
                assoc.secret = assoc.secret.split("b'")[1].split("'")[0]
                assoc.secret = bytes(assoc.secret, 'utf-8')
            association = OIDAssociation(assoc.handle,
                                         base64.decodestring(assoc.secret),
                                         assoc.issued, assoc.lifetime,
                                         assoc.assoc_type)
            expires = 0
            try:
                expires = association.getExpiresIn()
            except:
                expires = association.expiresIn
            if expires == 0:
                self.removeAssociation(server_url, assoc.handle)
            else:
                associations.append((association.issued, association))
        if not associations:
            return None
        return associations[-1][1]

    def removeAssociation(self, server_url, handle):
        assocs = list(
            OpenIDAssociation.objects.filter(
                server_url=server_url, handle=handle))
        assocs_exist = len(assocs) > 0
        for assoc in assocs:
            assoc.delete()
        return assocs_exist

    def useNonce(self, server_url, timestamp, salt):
        # Has nonce expired?
        if abs(timestamp - time.time()) > oidnonce.SKEW:
            return False
        try:
            nonce = OpenIDNonce.objects.get(
                server_url__exact=server_url,
                timestamp__exact=timestamp,
                salt__exact=salt)
        except OpenIDNonce.DoesNotExist:
            nonce = OpenIDNonce.objects.create(
                server_url=server_url, timestamp=timestamp, salt=salt)
            return True
        nonce.delete()
        return False

    def cleanupNonces(self):
        timestamp = int(time.time()) - oidnonce.SKEW
        OpenIDNonce.objects.filter(timestamp__lt=timestamp).delete()

    def cleanupAssociations(self):
        OpenIDAssociation.objects.extra(
            where=['issued + lifetimeint < (%s)' % time.time()]).delete()

    # pylint: disable=invalid-name
    def getAuthKey(self):
        # Use first AUTH_KEY_LEN characters of md5 hash of SECRET_KEY
        hash_object = hashlib.new('md5')
        hash_object.update(settings.SECRET_KEY)
        return hash_object.hexdigest()[:self.AUTH_KEY_LEN]