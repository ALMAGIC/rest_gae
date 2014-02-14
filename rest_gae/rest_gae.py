"""
Wraps NDB models and provided REST APIs (GET/POST/PUT/DELETE) arounds them.  Fully supports permissions.

Some code is taken from: https://github.com/abahgat/webapp2-user-accounts
"""

import importlib
import json
from datetime import datetime
from urllib import urlencode
import webapp2
import dateutil.parser
from google.appengine.ext import ndb
from google.appengine.ext.ndb import Cursor
from google.appengine.ext.db import BadValueError, BadRequestError
from webapp2_extras import auth
from webapp2_extras import sessions
from google.net.proto.ProtocolBuffer import ProtocolBufferDecodeError


# The REST permissions
PERMISSION_ANYONE = 'anyone'
PERMISSION_LOGGED_IN_USER = 'logged_in_user'
PERMISSION_OWNER_USER = 'owner_user'
PERMISSION_ADMIN = 'admin'



class NDBEncoder(json.JSONEncoder):
    """JSON encoding for NDB models and properties"""
    def _decode_key(self, key):
            model_class = ndb.Model._kind_map.get(key.kind())
            if getattr(model_class, 'RESTMeta', None) and getattr(model_class.RESTMeta, 'use_input_id', False):
                return key.string_id()
            else:
                return key.urlsafe()

    def default(self, obj):
        if isinstance(obj, ndb.Model):
            obj_dict = obj.to_dict()

            # Filter the properties that will be returned to user
            included_properties = get_included_properties(obj, 'output')
            obj_dict = dict((k,v) for k,v in obj_dict.iteritems() if k in included_properties)
            # Translate the property names
            obj_dict = translate_property_names(obj_dict, obj, 'output')
            obj_dict['id'] = self._decode_key(obj.key)

            return obj_dict

        elif isinstance(obj, datetime):
            return obj.isoformat()

        elif isinstance(obj, ndb.Key):
            return self._decode_key(obj)

        else:
            return json.JSONEncoder.default(self, obj)

class RESTException(Exception):
    """REST methods exception"""
    pass


#
# Utility functions
#


def translate_property_names(data, model, input_type):
    """Translates property names in `data` dict from one name to another, according to what is stated in `input_type` and the model's
    RESTMeta.translate_property_names/translate_input_property_names/translate_output_property_name - note that the change of `data` is in-place."""

    meta_class = getattr(model, 'RESTMeta', None)
    if not meta_class:
        return data

    translation_table = getattr(model.RESTMeta, 'translate_property_names', {})
    translation_table.update(getattr(model.RESTMeta, 'translate_%s_property_names' % input_type, {}))

    # Translate from one property name to another - for output, we turn the original property names
    # into the new property names. For input, we convert back from the new property names to the original
    # property names.
    for old_name, new_name in translation_table.iteritems():
        if input_type == 'output' and old_name not in data: continue
        if input_type == 'input' and new_name not in data: continue

        if input_type == 'output':
            original_value = data[old_name]
            del data[old_name]
            data[new_name] = original_value

        elif input_type == 'input':
            original_value = data[new_name]
            del data[new_name]
            data[old_name] = original_value

    return data

def get_included_properties(model, input_type):
    """Gets the properties of a `model` class to use for input/output (`input_type`). Uses the
    model's Meta class to determine the included/excluded properties."""

    meta_class = getattr(model, 'RESTMeta', None)

    included_properties = set()

    if meta_class:
        included_properties = set(getattr(meta_class, 'included_%s_properties' % input_type, []))
        included_properties.update(set(getattr(meta_class, 'included_properties', [])))

    if not included_properties:
        # No Meta class (or no included properties defined), assume all properties are included
        included_properties = set(model._properties.keys())

    if meta_class:
        excluded_properties = set(getattr(meta_class, 'excluded_%s_properties' % input_type, []))
        excluded_properties.update(set(getattr(meta_class, 'excluded_properties', [])))
    else:
        # No Meta class, assume no properties are excluded
        excluded_properties = set()

    # Add some default excluded properties
    if input_type == 'input':
        excluded_properties.update(set(BaseRESTHandler.DEFAULT_EXCLUDED_INPUT_PROPERTIES))
        if meta_class and getattr(meta_class, 'use_input_id', False):
            included_properties.update(['id'])
    if input_type == 'output':
        excluded_properties.update(set(BaseRESTHandler.DEFAULT_EXCLUDED_OUTPUT_PROPERTIES))

    # Calculate the properties to include
    properties = included_properties - excluded_properties

    return properties


def import_class(input_cls):
    """Imports a class (if given as a string) or returns as-is (if given as a class)"""

    if not isinstance(input_cls, str):
        # It's a class - return as-is
        return input_cls

    try:
        (module_name, class_name) = input_cls.rsplit('.', 1)
        module = __import__(module_name, fromlist=[class_name])
        return getattr(module, class_name)
    except Exception, exc:
        # Couldn't import the class
        raise ValueError("Couldn't import the model class '%s'" % input_cls)


class BaseRESTHandler(webapp2.RequestHandler):
    """Base request handler class for REST handlers (used by RESTHandlerClass and UserRESTHandlerClass)"""


    # The default number of results to return for a query in case `limit` parameter wasn't provided by the user
    DEFAULT_MAX_QUERY_RESULTS = 1000

    # The names of properties that should be excluded from input/output
    DEFAULT_EXCLUDED_INPUT_PROPERTIES = [ 'class_' ] # 'class_' is a PolyModel attribute
    DEFAULT_EXCLUDED_OUTPUT_PROPERTIES = [ ]


    #
    # Session related methods/properties
    #


    def dispatch(self):
        """Needed in order for the webapp2 sessions to work"""

        # Get a session store for this request.
        self.session_store = sessions.get_store(request=self.request)

        try:
            if getattr(self, 'allow_http_method_override', False) and ('X-HTTP-Method-Override' in self.request.headers):
                # User wants to override method type
                overridden_method_name = self.request.headers['X-HTTP-Method-Override'].upper().strip()
                if overridden_method_name not in ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']:
                    return self.method_not_allowed()

                self.request.method = overridden_method_name


            if getattr(self, 'allowed_origin', None):
                allowed_origin = self.allowed_origin

                if 'Origin' in self.request.headers:
                    # See if the origin matches
                    origin = self.request.headers['origin']

                    if (origin != allowed_origin) and (allowed_origin != '*'):
                        return self.permission_denied('Origin not allowed')


            # Dispatch the request.
            response = webapp2.RequestHandler.dispatch(self)

        except:
            raise
        else:
            # Save all sessions.
            self.session_store.save_sessions(response)

        return response


    @webapp2.cached_property
    def session(self):
        """Shortcut to access the current session."""
        return self.session_store.get_session(backend="datastore")



    #
    # Authentication methods/properties
    #


    @webapp2.cached_property
    def auth(self):
        """Shortcut to access the auth instance as a property."""
        return auth.get_auth()


    @webapp2.cached_property
    def user_info(self):
        """Shortcut to access a subset of the user attributes that are stored
        in the session.

        The list of attributes to store in the session is specified in
          config['webapp2_extras.auth']['user_attributes'].
        :returns
          A dictionary with most user information
        """
        return self.auth.get_user_by_session()

    @webapp2.cached_property
    def user_model(self):
        """Returns the implementation of the user model.

        It is consistent with config['webapp2_extras.auth']['user_model'], if set.
        """
        return self.auth.store.user_model

    @webapp2.cached_property
    def user(self):
        """Shortcut to access the current logged in user.

        Unlike user_info, it fetches information from the persistence layer and
        returns an instance of the underlying model.

        :returns
          The instance of the user model associated to the logged in user.
        """
        u = self.user_info
        return self.user_model.get_by_id(u['user_id']) if u else None


    #
    # HTTP response helper methods
    #


    def get_response(self, status, content):
        """Returns an HTTP status message with JSON-encoded content (and appropriate HTTP response headers)"""

        # Create the JSON-encoded response
        response = webapp2.Response(json.dumps(content, cls=NDBEncoder))

        response.status = status

        response.headers['Content-Type'] = 'application/json'
        response.headers['Access-Control-Allow-Methods'] = ', '.join(self.permissions.keys())

        if getattr(self, 'allowed_origin', None):
            response.headers['Access-Control-Allow-Origin'] = self.allowed_origin

        return response

    def success(self, content):
        return self.get_response(200, content)

    def error(self, exception):
        return self.get_response(400, {'error': str(exception)})

    def method_not_allowed(self):
        return self.get_response(405, {})

    def permission_denied(self, reason=None):
        return self.get_response(403, { 'reason': reason})

    def unauthorized(self):
        return self.get_response(401, {})

    def redirect(self, url):
        return webapp2.redirect(url)



    #
    # Utility methods
    #


    def _model_id_to_model(self, model_id):
        """Returns the model according to the model_id; raises an exception if invalid ID / model not found"""

        if not model_id:
            return None

        try:
            if getattr(self.model, 'RESTMeta', None) and getattr(self.model.RESTMeta, 'use_input_id', False):
                model = ndb.Key(self.model, model_id).get()
            else:
                model = ndb.Key(urlsafe=model_id).get()
            if not model: raise Exception()
        except Exception, exc:
            # Invalid key name
            raise RESTException('Invalid model id - %s' % model_id)

        return model


    def _build_next_query_url(self, cursor):
        """Returns the next URL to fetch results for - used when paging. Returns none if no more results"""
        if not cursor:
            return None

        # Use all of the original query arguments - just override the cursor argument
        params = self.request.GET
        params['cursor'] = cursor.urlsafe()
        return self.request.path_url + '?' + urlencode(params)

    def _filter_query(self):
        """Filters the query results for given property filters (if provided by user)."""

        if not self.request.GET.get('q'):
            # No query given - return as-is
            return self.model.query()

        try:
            return self.model.gql('WHERE ' + self.request.GET.get('q'))
        except Exception, exc:
            # Invalid query
            raise RESTException('Invalid query param - "%s"' % self.request.GET.get('q'))


    def _fetch_query(self, query):
        """Fetches the query results for a given limit (if provided by user) and for a specific results page (if given by user).
        Returns a tuple of (results, cursor_for_next_fetch). cursor_for_next_fetch will be None is no more results are available."""

        if not self.request.GET.get('limit'):
            # No limit given - use default limit
            limit = BaseRESTHandler.DEFAULT_MAX_QUERY_RESULTS
        else:
            try:
                limit = int(self.request.GET.get('limit'))
                if limit <= 0: raise ValueError('Limit cannot be zero or less')
            except ValueError, exc:
                # Invalid limit value
                raise RESTException('Invalid "limit" parameter - %s' % self.request.GET.get('limit'))

        if not self.request.GET.get('cursor'):
            # Fetch results from scratch
            cursor = None
        else:
            # Continue a previous query
            try:
                cursor = Cursor(urlsafe=self.request.GET.get('cursor'))
            except BadValueError, exc:
                raise RESTException('Invalid "cursor" argument - %s' % self.request.GET.get('cursor'))

        try:
            (results, cursor, more_available) = query.fetch_page(limit, start_cursor=cursor)
        except BadRequestError, exc:
            # This happens when we're using an existing cursor and the other query arguments were messed with
            raise RESTException('Invalid "cursor" argument - %s' % self.request.GET.get('cursor'))

        if not more_available:
            cursor = None

        return (results, cursor)


    def _order_query(self, query):
        """Orders the query if input given by user. Returns the modified, sorted query"""

        if not self.request.GET.get('order'):
            # No order given - return as-is
            return query

        try:
            # The order parameter is formatted as 'col1, -col2, col3'
            orders = [o.strip() for o in self.request.GET.get('order').split(',')]
            orders = ['+'+o if not o.startswith('-') and not o.startswith('+') else o for o in orders]
            orders = [-getattr(self.model, o[1:]) if o[0]=='-' else getattr(self.model, o[1:]) for o in orders]
        except AttributeError, exc:
            # Invalid column name
            raise RESTException('Invalid "order" parameter - %s' % self.request.GET.get('order'))

        # Return the ordered query
        return query.order(*orders)


    def _build_model_from_data(self, data, cls, model=None):
        """Builds a model instance (according to `cls`) from user input and returns it. Updates an existing model instance if given.
        Raises exceptions if input data is invalid."""

        # Translate the property names (this is done before the filtering in order to get the original property names by which the filtering is done)
        data = translate_property_names(data, cls, 'input')

        # Transform any raw input data into appropriate NDB properties - write all transformed properties
        # into another dict (so any other unauthorized properties will be ignored).
        input_properties = { }
        for (name, prop) in cls._properties.iteritems():
            if name not in data: continue # Input not given by user

            if prop._repeated:
                # This property is repeated (i.e. an array of values)
                input_properties[name] = [self._value_to_property(value, prop) for value in data[name]]
            else:
                input_properties[name] = self._value_to_property(data[name], prop)

        if not model and getattr(cls, 'RESTMeta', None) and getattr(cls.RESTMeta, 'use_input_id', False):
            if 'id' not in data:
                raise RESTException('id field is required')
            input_properties['id'] = data['id']

        # Filter the input properties
        included_properties = get_included_properties(cls, 'input')
        input_properties = dict((k,v) for k,v in input_properties.iteritems() if k in included_properties)

        # Set the user owner property to the currently logged-in user (if it's defined for the model class) - note that we're doing this check on the input `cls` parameter
        # and not the self.model class, since we need to support when a model has an inner StructuredProperty, and that model has its own RESTMeta definition.
        if hasattr(cls, 'RESTMeta') and hasattr(cls.RESTMeta, 'user_owner_property'):
            if not model and self.user:
                # Only perform this update when creating a new model - otherwise, each update might change this (very problematic in case an
                # admin updates another user's model instance - it'll change model ownership from that user to the admin)
                input_properties[cls.RESTMeta.user_owner_property] = self.user.key

        if not model:
            # Create a new model instance
            model = cls(**input_properties)
        else:
            # Update an existing model instance
            model.populate(**input_properties)

        return model

    def _value_to_property(self, value, prop):
        """Converts raw data value into an appropriate NDB property"""
        if isinstance(prop, ndb.KeyProperty):
            if value is None:
                return None
            try:
                return ndb.Key(urlsafe=value)
            except ProtocolBufferDecodeError as e:
                if prop._kind is not None:
                    model_class = ndb.Model._kind_map.get(prop._kind)
                    if getattr(model_class, 'RESTMeta', None) and getattr(model_class.RESTMeta, 'use_input_id', False):
                        return ndb.Key(model_class, value)
            raise RESTException('invalid key: {}'.format(value) )
        elif isinstance(prop, ndb.DateTimeProperty) or isinstance(prop, ndb.TimeProperty) or isinstance(prop, ndb.DateProperty):
            # TODO - use built-in datetime if dateutil is not found
            return dateutil.parser.parse(value)
        elif isinstance(prop, ndb.StructuredProperty):
            # It's a structured property - the input data is a dict - recursively parse it as well
            return self._build_model_from_data(value, prop._modelclass)
        else:
            # Return as-is (no need for further manipulation)
            return value




def get_rest_class(ndb_model, **kwd):
    """Returns a RESTHandlerClass with the ndb_model and permissions set according to input"""

    class RESTHandlerClass(BaseRESTHandler):

        model = import_class(ndb_model)
        permissions = { 'OPTIONS': PERMISSION_ANYONE }
        permissions.update(kwd.get('permissions', {}))
        allow_http_method_override = kwd.get('allow_http_method_override', True)
        allowed_origin = kwd.get('allowed_origin', None)

        # Wrapping in a list so the functions won't be turned into bound methods
        get_callback = [kwd.get('get_callback', None)]
        post_callback = [kwd.get('post_callback', None)]
        put_callback = [kwd.get('put_callback', None)]
        delete_callback = [kwd.get('delete_callback', None)]

        # Validate arguments (we do this at this stage in order to raise exceptions immediately rather than while the app is running)
        if PERMISSION_OWNER_USER in permissions.values():
            if not hasattr(model, 'RESTMeta') or not hasattr(model.RESTMeta, 'user_owner_property'):
                raise ValueError('Must define a RESTMeta.user_owner_property for the model class %s if user-owner permission is used' % (model))
            if not hasattr(model, model.RESTMeta.user_owner_property):
                raise ValueError('The user_owner_property "%s" (defined in RESTMeta.user_owner_property) does not exist in the given model %s' % (model.RESTMeta.user_owner_property, model))

        def __init__(self, request, response):
            self.initialize(request, response)

            self.get_callback = self.get_callback[0]
            self.post_callback = self.post_callback[0]
            self.put_callback = self.put_callback[0]
            self.delete_callback = self.delete_callback[0]


        def rest_method_wrapper(func):
            """Wraps GET/POST/PUT/DELETE methods and adds standard functionality"""

            def inner_f(self, model_id):
                # See if method type is supported
                method_name = func.func_name.upper()
                if method_name not in self.permissions:
                    return self.method_not_allowed()

                # Verify permissions
                permission = self.permissions[method_name]

                if (permission in [PERMISSION_LOGGED_IN_USER, PERMISSION_OWNER_USER, PERMISSION_ADMIN]) and (not self.user):
                    # User not logged-in as required
                    return self.unauthorized()

                elif permission == PERMISSION_ADMIN and not self.is_user_admin:
                    # User is not an admin
                    return self.permission_denied()

                try:
                    # Call original method
                    if model_id:
                        model = self._model_id_to_model(model_id[1:]) # Get rid of '/' at the beginning

                        if (permission == PERMISSION_OWNER_USER) and (self.get_model_owner(model) != self.user.key):
                            # The currently logged-in user is not the owner of the model
                            return self.permission_denied()

                        result = func(self, model)
                    else:
                        result = func(self, None)

                    return self.success(result)

                except RESTException, exc:
                    return self.error(exc)

            return inner_f


        #
        # REST endpoint methods
        #



        @rest_method_wrapper
        def options(self, model):
            """OPTIONS endpoint - doesn't return anything (only returns options in the HTTP response headers)"""
            return ''


        @rest_method_wrapper
        def get(self, model):
            """GET endpoint - retrieves a single model instance (by ID) or a list of model instances by query"""

            if not model:
                # Return a query with multiple results

                query = self._filter_query() # Filter the results

                if self.permissions['GET'] == PERMISSION_OWNER_USER:
                    # Return only models owned by currently logged-in user
                    query = query.filter(getattr(self.model, self.user_owner_property) == self.user.key)

                query = self._order_query(query) # Order the results
                (results, cursor) = self._fetch_query(query) # Fetch them (with a limit / specific page, if provided)

                if self.get_callback:
                    # Additional processing required
                    results = self.get_callback(results)

                return {
                    'results': results,
                    'next_results_url': self._build_next_query_url(cursor)
                    }

            else:
                # Return a single item (query by ID)

                if self.get_callback:
                    # Additional processing required
                    model = self.get_callback(model)

                return model


        @rest_method_wrapper
        def post(self, model):
            """POST endpoint - adds a new model instance"""

            if model:
                # Invalid usage of the endpoint
                raise RESTException('Cannot POST to a specific model ID')

            try:
                # Parse POST data as JSON
                json_data = json.loads(self.request.body)
            except ValueError, exc:
                raise RESTException('Invalid JSON POST data')

            try:
                # Any exceptions raised due to invalid/missing input will be caught
                model = self._build_model_from_data(json_data, self.model)

                if self.post_callback:
                    # Do some processing before saving the model
                    model = self.post_callback(model, json_data)

                model.put()
            except Exception, exc:
                raise RESTException('Invalid JSON POST data - %s' % exc)


            # Return the newly-created model instance
            return model


        @ndb.transactional(
                retries=0, # Don't re-try if we fail
                xg=True # We might touch several entity types during this process (in case of StructuredProperty)
                )
        def _multi_update_models(self, models):
            """Does updates for several models at once (each model is raw JSON data). It's a transactional method so if we fail for one model, we'll rollback all other changes."""

            permission = self.permissions['PUT']

            updated_models = []

            for model_to_update in models:
                if 'id' not in model_to_update: raise RESTException('Missing "id" argument for model')

                model_id = model_to_update['id']
                model = self._model_id_to_model(model_id) # Any exceptions raised here will be caught by calling function

                if (permission == PERMISSION_OWNER_USER) and (self.get_model_owner(model) != self.user.key):
                    # The currently logged-in user is not the owner of the model
                    raise RESTException('Model id %s is not owned by user' % model_id)

                # Update the current model
                try:
                    # Any exceptions raised due to invalid/missing input will be caught
                    model = self._build_model_from_data(model_to_update, self.model, model)

                    if self.put_callback:
                        # Do some processing before updating the model
                        model = self.put_callback(model, model_to_update)

                    model.put()

                    updated_models.append(model)
                except Exception, exc:
                    raise RESTException('Invalid JSON PUT data - %s' % exc)


            return updated_models


        @rest_method_wrapper
        def put(self, model):
            """PUT endpoint - updates an existing model instance"""

            try:
                # Parse PUT data as JSON
                json_data = json.loads(self.request.body)
            except ValueError, exc:
                raise RESTException('Invalid JSON PUT data')


            if not model:
                # Update several models at once

                if not isinstance(json_data, list):
                    raise RESTException('Invalid JSON PUT data')

                # Any exception raised here will be caught by calling function
                updated_models = self._multi_update_models(json_data)

                return updated_models



            #
            # Update a single model instance
            #

            try:
                # Any exceptions raised due to invalid/missing input will be caught
                model = self._build_model_from_data(json_data, self.model, model)

                if self.put_callback:
                    # Do some processing before updating the model
                    model = self.put_callback(model, json_data)

                model.put()
            except Exception, exc:
                raise RESTException('Invalid JSON PUT data - %s' % exc)


            # Return the updated model instance
            return model

        @rest_method_wrapper
        def delete(self, model):
            """DELETE endpoint - deletes an existing model instance"""

            if not model:
                # Delete multiple model instances

                if self.permissions['DELETE'] == PERMISSION_OWNER_USER:
                    # Delete all models owned by the currently logged-in user
                    query = self.model.query().filter(getattr(self.model, self.user_owner_property) == self.user.key)
                else:
                    # Delete all models
                    query = self.model.query()

                # Delete the models (we might need to fetch several pages in case of many results)
                cursor = None
                more_available = True
                deleted_models = []

                while more_available:
                    (results, cursor, more_available) = query.fetch_page(BaseRESTHandler.DEFAULT_MAX_QUERY_RESULTS, start_cursor=cursor)
                    if results:
                        if self.delete_callback:
                            # Since we need to call a callback function before each deletion, we can't use ndb.delete_multi

                            for m in results:
                                # Do some processing before deleting the model
                                self.delete_callback(m)
                                # Delete the current model
                                m.key.delete()

                                deleted_models.append(m)

                        else:
                            # Delete all models at once using ndb.delete_multi
                            ndb.delete_multi(m.key for m in results)
                            deleted_models += results

                # Return the deleted models
                return deleted_models


            #
            # Delete a single model
            #


            try:
                if self.delete_callback:
                    # Do some processing before deleting the model
                    self.delete_callback(model)

                model.key.delete()
            except Exception, exc:
                raise RESTException('Could not delete model - %s' % exc)


            # Return the deleted model instance
            return model

        #
        # Utility methods/properties
        #


        @webapp2.cached_property
        def is_user_admin(self):
            """Determines if the currently logged-in user is an admin or not (relies on the user class RESTMeta.admin_property)"""

            if not hasattr(self.user, 'RESTMeta') or not hasattr(self.user.RESTMeta, 'admin_property'):
                # This is caused due to a misconfiguration by the coder (didn't define a proper RESTMeta.admin_property) - we raise an exception so
                # it'll trigger a 500 internal server error. This specific argument validation is done here instead of the class definition (where the
                # rest of the arguments are being validated) since at that stage we can't see the webapp2 auth configuration to determine the User model.
                raise ValueError('The user model class %s must include a RESTMeta class with `admin_property` defined' % (self.user.__class__))

            admin_property = self.user.RESTMeta.admin_property
            if not hasattr(self.user, admin_property):
                raise ValueError('The user model class %s does not have the property %s as defined in its RESTMeta.admin_property' % (self.user.__class__, admin_property))

            return getattr(self.user, admin_property)

        @webapp2.cached_property
        def user_owner_property(self):
            """Returns the name of the user_owner_property"""
            return self.model.RESTMeta.user_owner_property

        def get_model_owner(self, model):
            """Returns the user owner of the given `model` (relies on RESTMeta.user_owner_property)"""
            return getattr(model, self.user_owner_property)





    # Return the class statically initialized with given input arguments
    return RESTHandlerClass


class RESTHandler(webapp2.Route):
    """Returns our RequestHandler with the appropriate permissions and model. Should be used as part of the WSGIApplication routing:
            app = webapp2.WSGIApplication([('/mymodel', RESTHandler(
                                                MyModel,
                                                permissions={
                                                    'GET': PERMISSION_ANYONE,
                                                    'POST': PERMISSION_LOGGED_IN_USER,
                                                    'PUT': PERMISSION_OWNER_USER,
                                                    'DELETE': PERMISSION_ADMIN
                                                },
                                                get_callback=lambda model: model,
                                                post_callback=lambda model, data: model,
                                                put_callback=lambda model, data: model,
                                                delete_callback=lambda model: model,
                                                allow_http_method_override=False,
                                                allowed_origin='*'
                                           )])


            Adds the following REST endpoints (according to `permissions` parameter):
                GET /mymodel - returns all instances of MyModel (PERMISSION_ANYONE - all instances; PERMISSION_OWNER_USER - only the ones owned by the current logged-in user)
                GET /mymodel/123 - returns information about a specific model instance (PERMISSION_OWNER_USER - only the owning user can view this information)
                POST /mymodel - create a new MyModel instance
                PUT /mymodel/123 - updates an existing model's properties (PERMISSION_OWNER_USER - only the owning user can do that)
                PUT /mymodel - updates several model instances at once. The entire request is transactional - If one of the model update fails, any previous updates
                                made in the same request will be undone.
                DELETE /mymodel/123 - deletes a specific model (PERMISSION_OWNER_USER - only the owning user can do that)
                DELETE /mymodel - PERMISSION_OWNER_USER: deletes all model instances owned by the currently logged-in user; PERMISSION_ADMIN - deletes all model instances

        Parameters:
            `model` - The ndb model class to be exposed (class or string)
            `permissions` - What REST methods should be exposed and to which users (admins or not)
                    This is a dict, where the key is one of 'GET', 'POST', 'PUT' or 'DELETE' and
                    the value is a string - one of the PERMISSION_ constants. If a method doesn't appear in this dict, it'll not be supported.
            `get_callback` - If set, this function will be called just before returning the results:
                    A) In case of a GET /mymodel - the argument will be a list of model instances. The function must return
                        a list of models, not necessarily the same as the input list (it can also be an empty list).
                    B) In case of a GET /mymodel/123 - the argument will be a single model instance. The function must return the model.
            `post_callback` - If set, this function will be called right after creating the model according to the input JSON data, and right before saving it (i.e. before model.put()).
                    The function receives two arguments: The model which will be saved; The raw input JSON dict (after it has gone through some pre-processing).
                    The function must return the model, in order for it to be saved. If the function raises an exception, the model creation fails with an error.
            `put_callback` - If set, this function will be called right after updating the model according to the input JSON data, and right before saving the updated model (i.e. before model.put()).
                    The function receives two arguments: The model which will be saved; The raw input JSON dict (after it has gone through some pre-processing).
                    The function must return the model, in order for it to be saved.
                    In case of multiple updates of models, this function will be called for each single model being updated.
                    If the function raises an exception, the model update fails with an error (in case of multi-update - the entire transaction fails).
            `delete_callback` - If set, this function will be called right before deleting a model. Receives an input argument of the model to be deleted. Function return value is ignored.
                    In case of multiple deletion of models, this function will be called for each single model being deleted.
                    If the function raises an exception, the model deletion fails with an error (in case of multi-delete - since there is no transaction, only the current deletion
                    will fail and all previously-successful deletions will remain the same).
            `allow_http_method_override` - (optional; default=True) If set, allows the user to add an HTTP request header 'X-HTTP-Method-Override' to override the request type (e.g.
                    if the HTTP request is a POST but it also contains 'X-HTTP-Method-Override: GET', it will be treated as a GET request).
            `allowed_origin` - (optional; default=None) If not set, CORS support is disabled. If set to '*' - allows Cross-Site HTTP requests from all domains;
                    if set to 'http://sub.example.com' or similar - allows Cross-Site HTTP requests only from that domain.
                    See https://developer.mozilla.org/en/docs/HTTP/Access_control_CORS for more information.


            NOTE: If using the PERMISSION_ADMIN, the authentication user model class MUST include a RESTMeta class with `admin_property` defined. For example:
                class MyUser(webapp2_extras.appengine.models.User):
                    is_admin = ndb.BooleanProperty(default=False)
                    class RESTMeta:
                        admin_property = 'is_admin'

            NOTE: If using PERMISSION_OWNER_USER, the input `model` class MUST include a RESTMeta class with a `user_owner_property` defined. That property will be
                    used in two cases:
                        A) When verifying the ownership of the model (e.g. PUT to a specific model that is not owned by the currently logged-in user).
                        B) When adding a new model (but not when updating) - that property will be assigned to the currently logged-in user. Note that this assignment works
                            recursively for any StructuredProperty of the model (if that StructuredProperty's model has its own `user_owner_property` defined).
                    For example:
                class MyModel(ndb.Model):
                    owner_user = ndb.KeyProperty(kind='MyUser')
                    class RESTMeta:
                        user_owner_property = 'owner_user'


            NOTE: You can define the accepted input for POST/PUT and accepted output for the various methods, by selecting which model properties are allowed:
                class MyModel(ndb.Model):
                    prop1 = ndb.StringProperty()
                    prop2 = ndb.StringProperty()
                    prop3 = ndb.StringProperty()

                    class RESTMeta:
                        excluded_input_properties = ['prop1'] # Ignore input from users for these properties (won't be changeable using PUT/POST)
                        excluded_output_properties = ['prop2'] # These properties won't be returned as output from the various endpoints
                        excluded_properties = [ ... ] # Excluded properties - Both input and output together

                        included_input_properties = ['prop1', 'prop3'] # Only these properties will be accepted as input from the user
                        included_output_properties = ['prop1', 'prop3'] # Only these properties will returned as output
                        included_properties = [ ... ] # Included properties - Both input and output together


            NOTE: You can define the names of properties, as they are displayed to the user and accepted as input:
                class MyModel(ndb.Model):
                    prop1 = ndb.StringProperty()
                    prop2 = ndb.StringProperty()
                    prop3 = ndb.StringProperty()

                    class RESTMeta:
                        # Any endpoint output will display 'prop1' as 'new_prop1' and 'prop3' as 'new_prop3'
                        translate_output_property_names = { 'prop1': 'new_prop1', 'prop3': 'new_prop3' }
                        # Any endpoint will receive as input 'new_prop2' instead of 'prop2'
                        translate_input_property_names = { 'prop2': 'new_prop2' }
                        translate_property_names = { ... } # Translation table - both for input and output

    """

    def __init__(self, url, model, **kwd):

        # Make sure we catch both URLs: to '/mymodel' and to '/mymodel/123'
        super(RESTHandler, self).__init__(url.rstrip(' /') + '<model_id:(/.+)?|/>', get_rest_class(model, **kwd))


