import hashlib
import commonware

from django.contrib.auth.models import User
from django.utils.encoding import smart_str
from django.conf import settings

from amo.helpers import get_amo_cursor
from person.models import Profile

DEFAULT_AMO_PASSWORD = 'saved in AMO'

log = commonware.log.getLogger('f.authentication')


class AMOAuthentication:

    def authenticate(self, username, password):
        """
            Authenticate user by contacting with AMO
        """

        # TODO: Validate alphanum + .-_@

        # check if username exists in database
        try:
            user = User.objects.get(username=username)
            # was user signed up via AMO?
            if user.password != DEFAULT_AMO_PASSWORD:
                # standard authorisation
                if user.check_password(password):
                    try:
                        profile = user.get_profile()
                    except:
                        # create empty profile for users stored in FD database
                        profile = Profile(user=user)
                        profile.save()
                    return user
                return None
        except User.DoesNotExist:
            # username does not exist in FD database
            user = None

        if not settings.AUTH_DATABASE:
            return None

        # here contact AMO and receive authentication status
        email = username
        username = self.auth_db_authenticate(username, password)

        if not username:
            return None

        # check if user was already signed to FD
        try:
            user = User.objects.get(username=username)
            # update user's email if needed
            if user.email != email:
                user.email = email
                user.save()
        except:
            # save user into the database
            user = User(
                username=username,
                email=email,
                password=DEFAULT_AMO_PASSWORD,
            )
            user.save()

        # Manage profile
        try:
            profile = user.get_profile()
        except Profile.DoesNotExist:
            profile = Profile(user=user)

        profile.update_from_AMO(self.user_data)
        return user

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except:
            return None

    def auth_db_authenticate(self, username, password):
        " authenticate email/password pair in AMO database "

        columns = ('id', 'email', 'username', 'display_name', 'password',
                   'homepage')
        auth_cursor = get_amo_cursor()

        SQL = ('SELECT %s FROM %s WHERE email=%%s') % (
                ','.join(columns), settings.AUTH_DATABASE['TABLE'])
        auth_cursor.execute(SQL, username)
        data = auth_cursor.fetchone()
        user_data = {}
        for i in range(len(data)):
            user_data[columns[i]] = data[i]
        if not user_data:
            return None

        if '$' not in user_data['password']:
            valid = (get_hexdigest('md5', '',
                                   password) == user_data['password'])
        else:
            algo, salt, hsh = user_data['password'].split('$')
            valid = (hsh == get_hexdigest(algo, salt, password))

        if not valid:
            return None

        username = user_data['id']
        self.user_data = user_data
        return username




def get_hexdigest(algorithm, salt, raw_password):
    return hashlib.new(algorithm, smart_str(salt + raw_password)).hexdigest()
