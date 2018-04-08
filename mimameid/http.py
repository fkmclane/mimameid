import base64
import hashlib
import http.client
import json
import logging
import os
import random
import string
import time
import urllib.parse
import uuid

import requests
import rsa

import fooster.web, fooster.web.file, fooster.web.form, fooster.web.json, fooster.web.page

import fooster.db

from mimameid import config


log = logging.getLogger('mimameid')

db = fooster.db.Database(config.dir + '/profiles.db', ['username', 'uuid', 'password', 'skin', 'cape', 'access', 'client'])
timeout = 3600
sessions = None

key = (None, None)


class Key(fooster.web.HTTPHandler):
    def do_get(self):
        return 200, key[0].save_pkcs1(format='DER')


class Index(fooster.web.page.PageHandler):
    directory = config.template
    page = 'index.html'


class Login(fooster.web.page.PageHandler, fooster.web.form.FormHandler):
    directory = config.template
    page = 'login.html'
    message = ''

    def format(self, page):
        return page.format(message=self.message)

    def do_post(self):
        try:
            username = self.request.body['username']
            password = self.request.body['password']
        except (KeyError, TypeError):
            self.response.headers['Location'] = '/'
            return 303, ''

        session = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(32))

        if username in db and hashlib.sha256(password.encode('utf-8')).hexdigest() == db[username].password:
            delete = []
            for session, user in sessions.items():
                if user[1] <= time.time():
                    delete.append(session)
            for session in delete:
                del sessions[session]

            sessions[session] = (username, time.time() + timeout)

            self.response.headers['Set-Cookie'] = 'session={}; Max-Age=3600'.format(session)
            self.response.headers['Location'] = '/edit'

            return 303, ''
        else:
            self.message = 'Username or password incorrect.'

        return self.do_get()


class Logout(fooster.web.HTTPHandler):
    def do_get(self):
        cookies = {cookie.split('=', 1)[0].strip(): cookie.split('=', 1)[1].strip() for cookie in self.request.headers['Cookie'].split(';')}

        try:
            delete = []
            for session, user in sessions.items():
                if user[1] <= time.time():
                    delete.append(session)
            for session in delete:
                del sessions[session]

            del sessions[cookies['session']]
        except (KeyError, IndexError):
            self.response.headers['Location'] = '/'
            return 303, ''

        self.response.headers['Set-Cookie'] = 'session=none; Max-Age=-1'
        self.response.headers['Location'] = '/login'

        return 303, ''


class Register(fooster.web.page.PageHandler, fooster.web.form.FormHandler):
    directory = config.template
    page = 'register.html'
    message = ''

    def format(self, page):
        return page.format(message=self.message)

    def do_post(self):
        try:
            username = self.request.body['username']
            password = self.request.body['password']
            confirm = self.request.body['confirm']
        except (KeyError, TypeError):
            self.response.headers['Location'] = '/'
            return 303, ''

        if password == confirm:
            if username not in db:
                db[username] = db.Entry(str(uuid.uuid4()).replace('-', ''), hashlib.sha256(password.encode('utf-8')).hexdigest(), '', '', '', '')

                self.response.headers['Location'] = '/login'

                return 303, ''
            else:
                self.message = 'Username already taken.'
        else:
            self.message = 'Passwords do not match.'

        return self.do_get()


class Edit(fooster.web.page.PageHandler, fooster.web.form.FormHandler):
    directory = config.template
    page = 'edit.html'
    message = ''

    def format(self, page):
        return page.format(username=self.username, message=self.message)

    def do_get(self):
        cookies = {cookie.split('=', 1)[0].strip(): cookie.split('=', 1)[1].strip() for cookie in self.request.headers['Cookie'].split(';')}

        try:
            delete = []
            for session, user in sessions.items():
                if user[1] <= time.time():
                    delete.append(session)
            for session in delete:
                del sessions[session]

            self.username = sessions[cookies['session']][0]
        except (KeyError, IndexError):
            self.response.headers['Location'] = '/'
            return 303, ''

        return super().do_get()

    def do_post(self):
        cookies = {cookie.split('=', 1)[0].strip(): cookie.split('=', 1)[1].strip() for cookie in self.request.headers['Cookie'].split(';')}

        try:
            self.username = sessions[cookies['session']][0]
        except (KeyError, IndexError):
            self.response.headers['Location'] = '/'
            return 303, ''

        if self.username not in db:
            self.response.headers['Location'] = '/login'
            return 303, ''

        user = db[self.username]

        if 'password' in self.request.body and self.request.body['password']:
            user.password = hashlib.sha256(self.request.body['password'].encode('utf-8')).hexdigest()

        if 'skin' in self.request.body and 'filename' in self.request.body['skin'] and self.request.body['skin']['filename']:
            skin = self.request.body['skin']['file'].read()

            if user.skin:
                for other in db:
                    if user.skin == other.skin or user.skin == other.cape:
                        break
                else:
                    os.unlink(os.path.join(config.dir, 'texture', user.skin))

            user.skin = hashlib.sha256(skin).hexdigest()

            os.makedirs(os.path.join(config.dir, 'texture'), exist_ok=True)
            with open(os.path.join(config.dir, 'texture', user.skin), 'wb') as skin_file:
                skin_file.write(skin)

        self.message = 'Successfully updated profile.'

        return self.do_get()


class Authenticate(fooster.web.json.JSONHandler):
    def do_post(self):
        try:
            username = self.request.body['username']

            try:
                user = db[username]
            except KeyError:
                if config.forward:
                    request = requests.post('https://authserver.mojang.com/authenticate', json=self.request.body)
                    return request.status_code, request.json()
                else:
                    raise fooster.web.HTTPError(403)

            if user.password != hashlib.sha256(self.request.body['password'].encode('utf-8')).hexdigest():
                raise fooster.web.HTTPError(403)

            user.access = ''.join(random.choice('1234567890abcdef') for _ in range(32))
            user.client = self.request.body['clientToken']

            data = {'accessToken': user.access, 'clientToken': user.client, 'availableProfiles': [{'id': user.uuid, 'name': user.username}], 'selectedProfile': {'id': user.uuid, 'name': user.username}}

            if 'requestUser' in self.request.body and self.request.body['requestUser']:
                data['user'] = {'id': user.uuid, 'properties': [{'name': 'preferredLanguage', 'value': 'en'}]}

            return 200, data
        except (KeyError, TypeError):
            raise fooster.web.HTTPError(400)



class Refresh(fooster.web.json.JSONHandler):
    def do_post(self):
        try:
            user = None

            for other in db:
                if other.client == self.request.body['clientToken']:
                    user = other
                    break

            if not user or not user.access or user.access != self.request.body['accessToken']:
                if config.forward:
                    request = requests.post('https://authserver.mojang.com/refresh', json=self.request.body)
                    return request.status_code, request.json()
                else:
                    raise fooster.web.HTTPError(403)

            user.access = ''.join(random.choice('1234567890abcdef') for _ in range(32))
            user.client = self.request.body['clientToken']

            data = {'accessToken': user.access, 'clientToken': user.client, 'availableProfiles': [{'id': user.uuid, 'name': user.username}], 'selectedProfile': {'id': user.uuid, 'name': user.username}}

            if 'requestUser' in self.request.body and self.request.body['requestUser']:
                data['user'] = {'id': user.uuid, 'properties': [{'name': 'preferredLanguage', 'value': 'en'}]}

            return 200, data
        except (KeyError, TypeError):
            raise fooster.web.HTTPError(400)


class Validate(fooster.web.json.JSONHandler):
    def do_post(self):
        try:
            user = None

            for other in db:
                if other.client == self.request.body['clientToken']:
                    user = other
                    break

            if not user or not user.access or user.access != self.request.body['accessToken'] or user.client != self.request.body['clientToken']:
                if config.forward:
                    request = requests.post('https://authserver.mojang.com/validate', json=self.request.body)
                    return request.status_code, None if request.status_code == 204 else request.json()
                else:
                    raise fooster.web.HTTPError(403)

            return 204, None
        except (KeyError, TypeError):
            raise fooster.web.HTTPError(400)


class Signout(fooster.web.json.JSONHandler):
    def do_post(self):
        try:
            username = self.request.body['username']

            try:
                user = db[username]
            except KeyError:
                if config.forward:
                    request = requests.post('https://authserver.mojang.com/signout', json=self.request.body)
                    return request.status_code, None if request.status_code == 204 else request.json()
                else:
                    raise fooster.web.HTTPError(403)

            if user.password != hashlib.sha256(self.request.body['password'].encode('utf-8')).hexdigest():
                raise fooster.web.HTTPError(403)

            user.access = ''

            return 204, None
        except (KeyError, TypeError):
            raise fooster.web.HTTPError(400)


class Invalidate(fooster.web.json.JSONHandler):
    def do_post(self):
        try:
            user = None

            for other in db:
                if other.client == self.request.body['clientToken']:
                    user = other
                    break

            if not user or not user.access or user.access != self.request.body['accessToken'] or user.client != self.request.body['clientToken']:
                if config.forward:
                    request = requests.post('https://authserver.mojang.com/invalidate', json=self.request.body)
                    return request.status_code, None if request.status_code == 204 else request.json()
                else:
                    raise fooster.web.HTTPError(403)

            user.access = ''

            return 204, None
        except (KeyError, TypeError):
            raise fooster.web.HTTPError(400)


class Profile(fooster.web.json.JSONHandler):
    def do_post(self):
        usernames = []
        forward = []

        for username in self.request.body:
            try:
                user = db[username]

                usernames.append({'id': user.uuid, 'name': user.username})
            except KeyError:
                forward.append(username)

        if config.forward:
            usernames.extend(requests.post('https://api.mojang.com/profiles/minecraft', json=forward).json())

        return 200, usernames


class Session(fooster.web.json.JSONHandler):
    def do_get(self):
        for other in db:
            if other.uuid == self.groups[0]:
                user = other
                break
        else:
            if config.forward:
                response = requests.get('https://sessionserver.mojang.com/session/minecraft/profile/' + self.groups[0] + self.groups[1])

                return response.status_code, response.json()
            else:
                raise fooster.web.HTTPError(404)

        textures = {'timestamp': int(round(time.time()*1000)), 'profileId': user.uuid, 'profileName': user.username, 'textures': {}}

        if user.skin:
            textures['textures']['SKIN'] = {'url': '{}/texture/{}'.format(config.service, user.skin)}

        if user.cape:
            textures['textures']['CAPE'] = {'url': '{}/texture/{}'.format(config.service, user.cape)}

        if self.groups[1] == '?unsigned=false':
            textures['signatureRequired'] = True

            textures_data = base64.b64encode(json.dumps(textures).encode('utf-8'))
            textures_signature = base64.b64encode(rsa.sign(textures_data, key[1], 'SHA-1'))

            return 200, {'id': user.uuid, 'name': user.username, 'properties': [{'name': 'textures', 'value': textures_data.decode(), 'signature': textures_signature.decode()}]}
        else:
            textures_data = base64.b64encode(json.dumps(textures).encode('utf-8'))

            return 200, {'id': user.uuid, 'name': user.username, 'properties': [{'name': 'textures', 'value': textures_data.decode()}]}


class Texture(fooster.web.file.FileHandler):
    def respond(self):
        norm_request = fooster.web.file.normpath(self.groups[0])
        if self.groups[0] != norm_request:
            self.response.headers.set('Location', '/texture' + norm_request)

            return 307, ''

        self.filename = config.dir + '/texture' + urllib.parse.unquote(self.groups[0])

        try:
            return super().respond()
        except fooster.web.HTTPError as error:
            if error.code == 404:
                conn = http.client.HTTPSConnection('textures.minecraft.net')
                conn.request('GET', '/texture/' + self.groups[0])
                response = conn.getresponse()

                return response.status, response
            else:
                raise


class Meta(fooster.web.json.JSONHandler):
    def do_get(self):
        request = requests.get('https://launchermeta.mojang.com/mc/game/' + self.groups[0])
        return request.status_code, request.json()


class Library(fooster.web.HTTPHandler):
    def do_get(self):
        conn = http.client.HTTPSConnection('libraries.minecraft.net')
        conn.request('GET', '/' + self.groups[0])
        response = conn.getresponse()

        return response.status, response


class JSONErrorHandler(fooster.web.json.JSONErrorHandler):
    def respond(self):
        if self.error.code == 405:
            self.error.message = {'error': 'Method Not Allowed', 'errorMessage': 'A non-POST request was received'}
        elif self.error.code == 404:
            self.error.message = {'error': 'Not Found', 'errorMessage': 'Requested resource was not found'}
        elif self.error.code == 403:
            self.error.message = {'error': 'ForbiddenOperationException', 'errorMessage': 'Request included invalid credentials'}
        elif self.error.code == 400:
            self.error.message = {'error': 'IllegalArgumentException', 'errorMessage': 'Request included invalid fields'}

        return super().respond()


web = None

routes = {}
error_routes = {}


routes.update({'/key': Key, '/': Index, '/login': Login, '/logout': Logout, '/register': Register, '/edit': Edit, '/authenticate': Authenticate, '/refresh': Refresh, '/validate': Validate, '/signout': Signout, '/invalidate': Invalidate, '/profiles/minecraft': Profile, '/session/minecraft/profile/([0-9a-f]{32})(\?.*)?': Session, '/texture/(.*)': Texture, '/mc/game/(.*)': Meta, '/(.*\.jar)': Library})
error_routes.update({'[0-9]{3}': JSONErrorHandler})


def start():
    global key, web, sessions

    if os.path.exists(config.dir + '/pub.key'):
        log.info('Loading RSA key...')

        with open(config.dir + '/pub.key', 'rb') as key_file:
            key_pub = rsa.PublicKey.load_pkcs1(key_file.read())
        with open(config.dir + '/priv.key', 'rb') as key_file:
            key_priv = rsa.PrivateKey.load_pkcs1(key_file.read())

        key = (key_pub, key_priv)
    else:
        log.info('Generating RSA key...')

        key = rsa.newkeys(2048)

        os.makedirs(config.dir, exist_ok=True)

        with open(config.dir + '/pub.key', 'wb') as key_file:
            key_file.write(key[0].save_pkcs1())
        with open(config.dir + '/priv.key', 'wb') as key_file:
            key_file.write(key[1].save_pkcs1())

    web = fooster.web.HTTPServer(config.addr, routes, error_routes)
    sessions = web.sync.dict()
    web.start()


def stop():
    global web, sessions

    web.stop()
    sessions = None
    web = None


def join():
    global web

    web.join()
