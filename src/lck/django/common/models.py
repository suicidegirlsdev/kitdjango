#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2011 by Łukasz Langa
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""lck.django.common.models
   ------------------------

   Contains a small set of useful abstract model base classes that are not
   application-specific.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import defaultdict
from datetime import datetime
from functools import partial
from hashlib import sha256
import re
import sys

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db import models as db
from django.forms import fields
from django.template.defaultfilters import urlencode
from django.utils.translation import ugettext
from django.utils.translation import ugettext_lazy as _
from dj.choices import Language

try:
    from django.utils.timezone import now
except ImportError:
    now = datetime.now

from lck.django.common import model_is_user, monkeys, nested_commit_on_success


EDITOR_TRACKABLE_MODEL = getattr(settings, 'EDITOR_TRACKABLE_MODEL', User)
MAC_ADDRESS_REGEX = re.compile(r'^([0-9a-fA-F]{2}([:-]?|$)){6}$')
DEFAULT_SAVE_PRIORITY = getattr(settings, 'DEFAULT_SAVE_PRIORITY', 0)
DIRTY_MARK = object()


class Named(db.Model):
    """Describes an abstract model with a unique ``name`` field."""

    name = db.CharField(verbose_name=_("name"), max_length=75, unique=True,
        db_index=True)

    class Meta:
        abstract = True

    def __unicode__(self):
        return self.name

    @property
    def name_urlencoded(self):
        """Useful as in {%url some-link argument.name_urlencoded%}."""
        return urlencode(self.name, safe="")

    class NonUnique(db.Model):
        """Describes an abstract model with a non-unique ``name`` field."""

        name = db.CharField(verbose_name=_("name"), max_length=75)

        class Meta:
            abstract = True

        def __unicode__(self):
            return self.name

        @property
        def name_urlencoded(self):
            """Useful as in {%url some-link argument.name_urlencoded%}."""
            return urlencode(self.name, safe="")


class Titled(db.Model):
    """Describes an abstract model with a unique ``title`` field."""

    title = db.CharField(verbose_name=_("title"), max_length=100, unique=True,
        db_index=True)

    class Meta:
        abstract = True

    def __unicode__(self):
        return self.title

    @property
    def title_urlencoded(self):
        return urlencode(self.title, safe="")

    class NonUnique(db.Model):
        """Describes an abstract model with a non-unique ``title`` field."""

        title = db.CharField(verbose_name=_("title"), max_length=100)

        class Meta:
            abstract = True

        def __unicode__(self):
            return self.title

        @property
        def title_urlencoded(self):
            return urlencode(self.title, safe="")


class Slugged(db.Model):
    """Describes an abstract model with a unique ``slug`` field."""

    slug = db.SlugField(verbose_name=_("permalink"), unique=True)

    class Meta:
        abstract = True

    def __unicode__(self):
        return self.slug

    class NonUnique(db.Model):
        """Describes an abstract model with a non-unique ``slug`` field."""

        slug = db.SlugField(verbose_name=_("permalink"))

        class Meta:
            abstract = True

        def __unicode__(self):
            return self.slug


class TimeTrackable(db.Model):
    """Describes an abstract model whose lifecycle is tracked by time. Includes
    a ``created`` field that is set automatically upon object creation,
    a ``modified`` field that is updated automatically upon calling ``save()``
    on the object whenever a **significant** change was done, and
    a ``cache_version`` integer field that is automatically incremeneted any
    time a **significant** change is done.

    By a **significant** change we mean any change outside of those internal
    ``created``, ``modified``, ``cache_version``, ``display_count``
    or ``last_active`` fields. Full list of ignored fields lies in
    ``TimeTrackable.insignificant_fields``.

    Note: for admin integration ``lck.django.common.admin.ModelAdmin`` is
    recommended over the vanilla ``ModelAdmin``. It adds the ``created`` and
    ``modified`` fields as filters on the side of the change list and those
    fields will be rendered as read-only on the change form."""

    insignificant_fields = {'cache_version', 'modified', 'modified_by',
        'display_count', 'last_active'}

    created = db.DateTimeField(verbose_name=_("date created"),
        default=now)
    modified = db.DateTimeField(verbose_name=_("last modified"),
        default=now)
    cache_version = db.PositiveIntegerField(verbose_name=_("cache version"),
        default=0, editable=False)

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        super(TimeTrackable, self).__init__(*args, **kwargs)
        self._update_field_state()

    def save(self, update_modified=True, *args, **kwargs):
        """Overrides save(). Adds the ``update_modified=True`` argument.
        If False, the ``modified`` field won't be updated even if there were
        **significant** changes to the model."""
        if self.significant_fields_updated:
            self.cache_version += 1
            if update_modified:
                self.modified = now()
        super(TimeTrackable, self).save(*args, **kwargs)
        self._update_field_state()

    def update_cache_version(self, force=False):
        """Updates the cache_version bypassing the ``save()`` mechanism, thus
        providing better performance and consistency. Unless forced by
        ``force=True``, the update happens only when a **significant** change
        was made on the object."""
        if force or self.significant_fields_updated:
            # we're not using save() to bypass signals etc.
            self.__class__.objects.filter(pk = self.pk).update(cache_version=
                db.F("cache_version") + 1)

    def _update_field_state(self):
        self._field_state = self._fields_as_dict()

    def _fields_as_dict(self):
        fields = []
        for f in self._meta.fields:
            _name = f.name
            if f.rel:
                _name += '_id'
            fields.append((_name, getattr(self, _name)))
        return dict(fields)

    @property
    def significant_fields_updated(self):
        """Returns True on significant changes to the model.

        By a **significant** change we mean any change outside of those internal
        ``created``, ``modified``, ``cache_version``, ``display_count``
        or ``last_active`` fields. Full list of ignored fields lies in
        ``TimeTrackable.insignificant_fields``."""
        return bool(set(self.dirty_fields.keys()) - self.insignificant_fields)

    @property
    def dirty_fields(self):
        """dirty_fields() -> {'field1': 'old_value1', 'field2': 'old_value2', ...}

        Returns a dictionary of attributes that have changed on this object
        and are not yet saved. The values are original values present in the
        database at the moment of this object's creation/read/last save."""
        new_state = self._fields_as_dict()
        diff = []
        for k, v in self._field_state.iteritems():
            try:
                if v == new_state.get(k):
                    continue
            except (TypeError, ValueError):
                pass # offset-naive and offset-aware datetimes, etc.
            if v is DIRTY_MARK:
                v = new_state.get(k)
            diff.append((k, v))
        return dict(diff)

    def mark_dirty(self, *fields):
        """Forces `fields` to be marked as dirty to make all machinery checking
        for dirty fields treat them accordingly."""
        _dirty_fields = self.dirty_fields
        for field in fields:
            if field in _dirty_fields:
                continue
            self._field_state[field] = DIRTY_MARK

    def mark_clean(self, *fields, **kwargs):
        """Removes the forced dirty marks from fields.

        Fields that would be considered dirty anyway stay that way, unless
        `force` is set to True. In that case a field is unmarked until another
        change on it happens."""
        force = kwargs.get('force', False)
        _dirty_fields = self.dirty_fields
        _current_state = self._fields_as_dict()
        for field in fields:
            if field not in _dirty_fields:
                continue
            if self._field_state[field] is DIRTY_MARK:
                self._field_state[field] = _dirty_fields[field]
            elif force:
                self._field_state[field] = _current_state[field]


class SavePrioritized(TimeTrackable):
    """Describes a variant of the ``TimeTrackable`` model which also tracks
    priorities of saves on its fields. The default priority is stored in the
    settings under ``DEFAULT_SAVE_PRIORITY`` (defaults to 0 if missing).

    The priority engine enables parts of the application to make saves with
    higher priority. Then all modified fields store the priority value required
    to update them again. If another part of the application comes along to
    update one of those fields using lower priority, the change is silently
    dropped. The priority engine works on a per-field basis.

    Note: This base class should be put as far in the MRO as possible
    to protect from saving transformations of values which would be ignored
    otherwise.

    Note: Because of the limits of Django's multiple inheritance support,
    models based on ``SavePrioritized`` **CAN NOT** also be explicitly based on
    ``TimeTrackable``. They are based implicitly so this should be no problem.
    Just make sure you get rid of the ``TimeTrackable`` base class if you
    introduce ``SavePrioritized``."""

    insignificant_fields = TimeTrackable.insignificant_fields | {
        'save_priorities', 'max_save_priority'}

    save_priorities = db.TextField(verbose_name=_("save priorities"),
        default="", editable=False)
    max_save_priority = db.PositiveIntegerField(
        verbose_name=_("highest save priority"), default=0, editable=False)

    class Meta(TimeTrackable.Meta):
        abstract = True

    def get_save_priorities(self):
        """Decodes the stored ``save_priorities`` and returns a defaultdict
        (a key miss returns priority 0).

        Probably an internal state method, not that much interesting for
        typical model consumers."""
        result = defaultdict(lambda: 0)
        for token in self.save_priorities.split(" "):
            try:
                name, priority = token.split("=")
                result[name] = int(priority)
            except ValueError:
                continue
        return result

    def update_save_priorities(self, priorities):
        """Encodes the ``save_priorities`` field based on the provided
        ``priorities`` dictionary (like -but not limited to- the one returned
        by ``get_save_priorities()``).

        Note: this doesn't automatically run save() after updating the
        ``save_priorities`` field.

        Probably an internal state method, not that much interesting for
        typical model consumers."""
        tokens = []
        for field, priority in priorities.iteritems():
            if priority == 0:
                continue
            tokens.append("{}={}".format(field, priority))
        self.save_priorities = " ".join(tokens)

    def save(self, priority=DEFAULT_SAVE_PRIORITY, *args, **kwargs):
        """Overrides save(), adding the ``priority=DEFAULT_SAVE_PRIORITY``
        argument. Non-zero priority changes to **significant** fields are
        annotated and saved with the object. If later on another writer with
        lower priority changes one of those fields which were modified with
        higher priority, the later change is silently rolled back.

        Note: priorities are stored and enforced on a per-field level.
        """
        priorities = self.get_save_priorities()
        if self.significant_fields_updated and \
            priority > self.max_save_priority:
            self.max_save_priority = priority
        for field, orig_value in self.dirty_fields.iteritems():
            if field in self.insignificant_fields:
                # ignore insignificant fields
                continue
            if priorities[field] <= priority:
                priorities[field] = priority
            else:
                # undo the change if priority too low
                setattr(self, field, orig_value)
        self.update_save_priorities(priorities)
        super(SavePrioritized, self).save(*args, **kwargs)
        # FIXME: should this restore the values that were not saved or not?


class ImageModel(Titled, Slugged, TimeTrackable):
    """Describes image objects. Usage::

        class Icon(ImageModel):
            image = ImageModel.image_field(upload_to='icons', etc.)
    """
    height = db.PositiveIntegerField(verbose_name=_("height"), default=0,
            editable=False)
    width = db.PositiveIntegerField(verbose_name=_("width"), default=0,
            editable=False)
    image_field = partial(db.ImageField, verbose_name=_("file"),
            height_field='height', width_field='width', max_length=200)

    def __unicode__(self):
        if not self.image:
            return ugettext("new image")
        format = self.title, self.width, self.height, self.image.size/1024
        return "%s (%dx%d, %d kB)" % format

    class Meta:
        abstract = True


class EditorTrackable(db.Model):
    """Describes objects authored by users of the application. In the admin,
    on object creation the ``created_by`` field is set according to the editor.
    Same goes for modifying an object and the ``modified_by`` field. Works best
    in integration with TimeTrackable.

    If you would rather link to your user profile instead of the user object
    directly, use the ``EDITOR_TRACKABLE_MODEL`` (the same
    ``"app_name.ModelClass"``syntax as ``AUTH_PROFILE_MODULE``) setting.

    Note: for automatic editor updates in admin,
    ``lck.django.common.admin.ModelAdmin`` **MUST** be used instead of the
    vanilla ``ModelAdmin``. As a bonus, the ``created_by`` and ``modified_by``
    fields will appear as filters on the side of the change list
    and those fields will be rendered as read-only on the change form."""
    created_by = db.ForeignKey(EDITOR_TRACKABLE_MODEL,
        verbose_name=_("created by"), null=True, blank=True, default=None,
        related_name='+', on_delete=db.SET_NULL,
        limit_choices_to={'is_staff' if model_is_user(EDITOR_TRACKABLE_MODEL)
            else 'user__is_staff': True})
    modified_by = db.ForeignKey(EDITOR_TRACKABLE_MODEL,
        verbose_name=_("modified by"), null=True, blank=True, default=None,
        related_name='+', on_delete=db.SET_NULL,
        limit_choices_to={'is_staff' if model_is_user(EDITOR_TRACKABLE_MODEL)
            else 'user__is_staff': True})

    class Meta:
        abstract = True

    def get_editor_from_request(self, request):
        """This has to be overriden if you're using a custom editor model.
        Both ``auth.User`` and ``AUTH_PROFILE_MODULE`` are automatically
        handled."""
        if model_is_user(EDITOR_TRACKABLE_MODEL):
            return request.user
        else:
            return request.user.get_profile()

    def pre_save_model(self, request, obj, form, change):
        """Internal method used by ``lck.django.common.ModelAdmin``."""
        if not change:
            if not obj.created_by:
                obj.created_by = self.get_editor_from_request(request)
        else:
            obj.modified_by = self.get_editor_from_request(request)


class DisplayCounter(db.Model):
    """Describes an abstract model which `display_count` can be incremented by
    calling ``bump()``.

    If ``bump()`` is called with some `unique_id` as its argument, Django's
    cache will be used to ensure subsequent invocations with the same
    `unique_id` won't bump the display count. This functionality requires
    `get_absolute_url()` to be defined for the model.

    If the model is also ``TimeTrackable``, bumps won't update the `modified`
    field.
    """
    display_count = db.PositiveIntegerField(verbose_name=_("display count"),
        default=0, editable=False)

    class Meta:
        abstract = True

    def bump(self, unique_id=None):
        """bump([unique_id])

        Increments the ``display_count`` field. If ``unique_id`` is provided,
        Django's cache is used to make sure a unique visitor can only increment
        this counter once per hour. Recommended to be used in the form::

          model.bump(remote_addr(request))

        where ``remote_addr`` is a helper from ``lck.django.common``."""
        should_update = True
        if unique_id:
            if not hasattr(self, 'get_absolute_url'):
                raise ImproperlyConfigured("{} model doesn't define "
                    "get_absolute_url() required for DisplayCounter.bump() "
                    "to work with a `unique_id` argument.")
            url = self.get_absolute_url()
            hash = sha256(url).hexdigest()
            unique_id = sha256(str(unique_id)).hexdigest()
            key = "displaycounter::bump::{}::{}".format(hash, unique_id)
            should_update = not bool(cache.get(key))
            cache.set(key, True, 60*60)
        if should_update:
            # we're not using save() to bypass signals etc.
            self.__class__.objects.filter(pk = self.pk).update(display_count=
                db.F("display_count") + 1)


class ViewableSoftDeletableManager(db.Manager):
    """An object manager to automatically hide objects that were soft deleted
    for models inheriting ``SoftDeletable``."""

    def get_query_set(self):
        # get the original query set
        query_set = super(ViewableSoftDeletableManager, self).get_query_set()
        # leave rows which are deleted
        query_set = query_set.filter(deleted=False)
        return query_set


class SoftDeletable(db.Model):
    """
    Describes an abstract models which can be soft deleted, that is instead of
    actually removing objects from the database, they have a ``deleted`` field
    which is set to ``True`` and the object is then invisible in normal
    operations (thanks to ``ViewableSoftDeletableManager``).
    """
    deleted = db.BooleanField(verbose_name=_("deleted"), default=False,
        help_text=_("if selected, this element is not available on the "
        "website"), db_index=True)
    admin_objects = db.Manager()
    objects = ViewableSoftDeletableManager()

    class Meta:
        abstract = True


class WithConcurrentGetOrCreate(object):
    """
    The built-in ``Model.objects.get_or_create()`` doesn't work well in
    concurrent environments. This mixin solves the problem by trying to INSERT
    first and only if it fails, SELECT an existing row.

    This version is also more strict in terms of acceptable arguments.
    Arguments passed directly to ``concurrent_get_or_create`` should be unique
    fields or fields forming a ``unique_together`` constraint. Passing other
    fields will raise an AssertionError. Those fields can be optionally given
    in the ``defaults`` argument.

    Note: inherently incompatible with nested_commit_on_success (will commit
    underlying transactions).
    """
    @classmethod
    @nested_commit_on_success
    def concurrent_get_or_create(cls, **kwargs):
        assert kwargs, ('concurrent_get_or_create() must be passed at least '
                        'one keyword argument')
        defaults = kwargs.pop('defaults', {})
        required_fields = {f.name for f in cls._meta.fields if f.unique}
        unique_together = {}
        for fieldset in cls._meta.unique_together:
            for field in fieldset:
                unique_together.setdefault(field, set()).update(fieldset)
        unique_together = {f: set(fs) for fs in cls._meta.unique_together
                           for f in fs}
        for f in cls._meta.fields:
            if f.attname in kwargs:
                kwargs[f.name] = kwargs.pop(f.attname)
        given_fields = set(kwargs.keys())
        spurious_fields = given_fields - required_fields
        not_spurious_actually = set()
        for field in spurious_fields:
            if field not in unique_together:
                continue
            missing_fields = unique_together[field] - spurious_fields
            assert not missing_fields, ("Missing unique_together fields: "
                                        "{}".format(missing_fields))
            not_spurious_actually.add(field)
        spurious_fields -= not_spurious_actually
        assert not spurious_fields, ("Spurious fields given. Move those to "
                                     "`defaults`: {}".format(spurious_fields))
        try:
            params = dict(kwargs)
            params.update(defaults)
            return cls.objects.create(**params), True
        except IntegrityError:
            transaction.commit()
            exc_info = sys.exc_info()
            try:
                return cls.objects.get(**kwargs), False
            except (cls.DoesNotExist, cls.MultipleObjectsReturned):
                # DoesNotExist: there's a partial argument match in the DB
                # MultipleObjectsReturned: not enough unique arguments given
                raise exc_info[1], None, exc_info[2]


class VerboseNameGetter(object):
    def __init__(self, model):
        self.model = model

    def __hasattr__(self, name):
        try:
            name = self.model._meta.get_field_by_name(name)[0].verbose_name
            return True
        except:
            return False

    def __getattr__(self, name):
        try:
            return self.model._meta.get_field_by_name(name)[0].verbose_name
        except:
            return None


class MACAddressFormField(fields.RegexField):
    default_error_messages = {
        'invalid': _(u'Enter a valid MAC address.'),
    }

    def __init__(self, *args, **kwargs):
        super(MACAddressFormField, self).__init__(MAC_ADDRESS_REGEX,
            *args, **kwargs)


class MACAddressField(db.Field):
    empty_strings_allowed = False
    allowed_characters = "0123456789ABCDEF"

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 17
        super(MACAddressField, self).__init__(*args, **kwargs)

    def get_internal_type(self):
        return "CharField"

    def formfield(self, **kwargs):
        defaults = {'form_class': MACAddressFormField}
        defaults.update(kwargs)
        return super(MACAddressField, self).formfield(**defaults)

    @classmethod
    def normalize(cls, value):
        if not value:
            return None
        for sep in ':-.':
            _parts = value.split(sep, 6)
            if len(_parts) == 6:
                parts = []
                for part in _parts:
                    p = part.strip().lstrip('0').upper()
                    if len(p) == 0:
                        p = '00'
                    elif len(p) == 1:
                        p = '0' + p
                    elif len(p) > 2:
                        raise ValueError("Invalid octet '{}' in MAC address: "
                            "'{}'".format(p, value))
                    parts.append(p)
                break # found
        else:
            value = value.strip()
            if len(value) == 12:
                parts = [value.upper()]
            elif not len(value):
                return None
            else:
                raise ValueError("Invalid MAC address: '{}'".format(value))
        result = ''.join(p for p in parts)
        for char in result:
            if char not in cls.allowed_characters:
                raise ValueError("Invalid MAC address: '{}'".format(value))
        return result or None

    def get_prep_value(self, value):
        return self.normalize(value)

    def south_field_triple(self):
        kwargs = dict(
            null=repr(self.null),
            blank=repr(self.blank),
            db_column=repr(self.db_column),
            db_index=repr(self.db_index),
            primary_key=repr(self.primary_key),
            unique=repr(self.unique),
        )
        if self.default is not db.NOT_PROVIDED:
            kwargs['default'] = repr(self.default)
        return ('lck.django.common.models.MACAddressField', [], kwargs)
