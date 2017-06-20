# -*- coding: utf-8 -*-
# Copyright 2017 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

"""
Events
======

Events are a notification system.

On one side, one or many listeners await for an event to happen. On
the other side, when such event happen, a notification is sent to
the listeners.

An example of event is: 'when a record has been created'.

The event system allows to write the notification code in only one place, in
one Odoo addon, and to write as many listeners as we want, in different places,
different addons.

We'll see below how the ``on_record_create`` is implemented.

Notifier
--------

The first thing is to find where/when the notification should be sent.
For the creation of a record, it is in :meth:`odoo.models.BaseModel.create`.
We can inherit from the `'base'` model to add this line:

::

    class Base(models.AbstractModel):
        _inherit = 'base'

        @api.model
        def create(self, vals):
            record = super(Base, self).create(vals)
            self._event('on_record_create').notify(record, fields=vals.keys())
            return record

The :meth:`..models.base.Base._event` method has been added to the `'base'`
model, so an event can be notified from any model. The
:meth:`CollectedEvents.notify` method triggers the event and forward the
arguments to the listeners.

This should be done only once.

Listeners
---------

Listeners are Components that respond to the event names.
The components must have a ``_usage`` equals to ``'event.listener'``, but it
doesn't to be set manually if the component inherits from
``'base.event.listener'``

Here is how we would log something each time a record is created::

    class MyEventListener(Component):
        _name = 'my.event.listener'
        _inherit = 'base.event.listener'

        def on_record_create(self, record, fields=None):
            _logger.info("%r has been created", record)

Many listeners such as this one could be added for the same event.


Collection and models
---------------------

In the example above, the listeners is global. It will be executed for any
model and collection. You can also restrict a listener to only a collection or
model, using the ``_collection`` or ``_apply_on`` attributes.

::

    class MyEventListener(Component):
        _name = 'my.event.listener'
        _inherit = 'base.event.listener'
        _collection = 'magento.backend'

        def on_record_create(self, record, fields=None):
            _logger.info("%r has been created", record)


    class MyModelEventListener(Component):
        _name = 'my.event.listener'
        _inherit = 'base.event.listener'
        _apply_on = ['res.users']

        def on_record_create(self, record, fields=None):
            _logger.info("%r has been created", record)


If you want an event to be restricted to a collection, the
notification must also precise the collection, otherwise all listeners
will be executed::


    collection = self.env['magento.backend']
    self._event('on_foo_created', collection=collection).notify(record, vals)


"""

import logging
import operator

from odoo.addons.component.core import AbstractComponent, Component

_logger = logging.getLogger(__name__)

try:
    from cachetools import LRUCache, cachedmethod, keys
except ImportError:
    _logger.debug("Cannot import 'cachetools'.")

# Number of items we keep in LRU cache when we collect the events.
# 1 item means: for an event name, model_name, collection, return
# the event methods
DEFAULT_EVENT_CACHE_SIZE = 512


class CollectedEvents(object):
    """ Event methods ready to be notified

    This is a rather internal class. An instance of this class
    is prepared by the :class:`EventCollecter` when we need to notify
    the listener that the event has been triggered.

    :meth:`EventCollecter.collect_events` collects the events,
    feed them to the instance, so we can use the :meth:`notify` method
    that will forward the arguments and keyword arguments to the
    listeners of the event.
    ::

        >>> # collecter is an instance of CollectedEvents
        >>> collecter.collect_events('on_record_create').notify(something)

    """

    def __init__(self, events):
        self.events = events

    def notify(self, *args, **kwargs):
        """ Forward the arguments to every listeners of an event """
        for event in self.events:
            event(*args, **kwargs)


class EventCollecter(Component):
    """ Component that collects the event from an event name

    For doing so, it searches all the components that respond to the
    ``event.listener`` ``_usage`` and having an event of the same
    name.

    Then it feeds the events to an instance of :class:`EventCollecter`
    and return it to the caller.

    It keeps the results in a cache, the Component is rebuilt when
    the Odoo's registry is rebuilt, hence the cache is cleared as well.

    An event always starts with ``on_``.

    Note that the special
    :class:`..core.EventWorkContext` class should be
    used for this Component, because it can work
    without a collection.

    It is used by :meth:`..models.base.Base._event`.

    """
    _name = 'base.event.collecter'

    @classmethod
    def _complete_component_build(cls):
        """ Create a cache on the class when the component is built """
        super(EventCollecter, cls)._complete_component_build()
        # the _cache being on the component class, which is
        # dynamically rebuild when odoo registry is rebuild, we
        # are sure that the result is always the same for a lookup
        # until the next rebuild of odoo's registry
        cls._cache = LRUCache(maxsize=DEFAULT_EVENT_CACHE_SIZE)

    @cachedmethod(operator.attrgetter('_cache'),
                  key=lambda self, name: keys.hashkey(
                      self.work.collection._name
                      if self.work._collection is not None else None,
                      self.work.model_name,
                      name)
                  )
    def _collect_events(self, name):
        events = set([])
        collection_name = (self.work.collection._name
                           if self.work._collection is not None
                           else None)
        component_classes = self.work.components_registry.lookup(
            collection_name=collection_name,
            usage='event.listener',
            model_name=self.work.model_name,
        )
        for cls in component_classes:
            if cls.has_event(name):
                component = cls(self.work)
                events.add(getattr(component, name))
        return events

    def collect_events(self, name):
        """ Collect the events of a given name """
        if not name.startswith('on_'):
            raise ValueError("an event name always starts with 'on_'")

        events = self._collect_events(name)
        return CollectedEvents(events)


class EventListener(AbstractComponent):
    """ Base Component for the Event listeners

    Events must be methods starting with ``on_``.

    Example: :class:`RecordsEventListener`

    """
    _name = 'base.event.listener'
    _usage = 'event.listener'

    @classmethod
    def has_event(cls, name):
        """ Indicate if the class has an event of this name """
        return name in cls._events

    @classmethod
    def _build_event_listener_component(cls):
        """ Make a list of events listeners for this class """
        events = set([])
        if not cls._abstract:
            for attr_name in dir(cls):
                if attr_name.startswith('on_'):
                    events.add(attr_name)
        cls._events = events

    @classmethod
    def _complete_component_build(cls):
        super(EventListener, cls)._complete_component_build()
        cls._build_event_listener_component()


class RecordsEventListener(AbstractComponent):
    """ Abstract Component with base events """
    _name = 'records.event.listener'
    _inherit = 'base.event.listener'

    def on_record_create(self, record, fields=None):
        """ Called when a record is created """

    def on_record_write(self, record, fields=None):
        """ Called when a record is modified """

    def on_record_unlink(self, record):
        """ Called when a record is deleted """
