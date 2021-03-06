#     Copyright 2014 Netflix, Inc.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""
.. module: security_monkey.datastore
    :platform: Unix
    :synopsis: Contains the SQLAlchemy models and a few helper methods.

.. version:: $$VERSION$$
.. moduleauthor:: Patrick Kelley <pkelley@netflix.com> @monkeysecurity

"""
from flask_security.core import UserMixin, RoleMixin
from flask_security.signals import user_registered
from sqlalchemy import BigInteger

from auth.models import RBACUserMixin

from security_monkey import db, app

from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Unicode, Text
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.schema import ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship, backref

from sqlalchemy.orm import deferred

from copy import deepcopy
import dpath.util
from dpath.exceptions import PathNotFound
from security_monkey.common.utils import sub_dict

import datetime
import json
import hashlib
import traceback


association_table = db.Table(
    'association',
    Column('user_id', Integer, ForeignKey('user.id')),
    Column('account_id', Integer, ForeignKey('account.id'))
)


class Account(db.Model):
    """
    Meant to model AWS accounts.
    """
    __tablename__ = "account"
    id = Column(Integer, primary_key=True)
    active = Column(Boolean())
    third_party = Column(Boolean())
    name = Column(String(32))
    notes = Column(String(256))
    s3_name = Column(String(64))
    number = Column(String(12))  # Not stored as INT because of potential leading-zeros.
    items = relationship("Item", backref="account", cascade="all, delete, delete-orphan")
    issue_categories = relationship("AuditorSettings", backref="account")
    role_name = Column(String(256))

    exceptions = relationship("ExceptionLogs", backref="account", cascade="all, delete, delete-orphan")


class Technology(db.Model):
    """
    meant to model AWS primitives (elb, s3, iamuser, iamgroup, etc.)
    """
    __tablename__ = 'technology'
    id = Column(Integer, primary_key=True)
    name = Column(String(32))  # elb, s3, iamuser, iamgroup, etc.
    items = relationship("Item", backref="technology")
    issue_categories = relationship("AuditorSettings", backref="technology")
    ignore_items = relationship("IgnoreListEntry", backref="technology")

    exceptions = relationship("ExceptionLogs", backref="technology", cascade="all, delete, delete-orphan")


roles_users = db.Table(
    'roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id'))
)


class Role(db.Model, RoleMixin):
    """
    Used by Flask-Login / the auth system to check user permissions.
    """
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))


class User(UserMixin, db.Model, RBACUserMixin):
    """
    Used by Flask-Security and Flask-Login.
    Represents a user of Security Monkey.
    """
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    active = db.Column(db.Boolean())
    confirmed_at = db.Column(db.DateTime())
    daily_audit_email = Column(Boolean())
    change_reports = Column(String(32))  # All, OnlyWithIssues, None

    # Flask-Security SECURITY_TRACKABLE
    last_login_at = Column(DateTime())
    current_login_at = Column(DateTime())
    login_count = Column(Integer)
    # Why 45 characters for IP Address ?
    # See http://stackoverflow.com/questions/166132/maximum-length-of-the-textual-representation-of-an-ipv6-address/166157#166157
    last_login_ip = Column(db.String(45))
    current_login_ip = Column(db.String(45))

    accounts = relationship("Account", secondary=association_table)
    item_audits = relationship("ItemAudit", uselist=False, backref="user")
    revision_comments = relationship("ItemRevisionComment", backref="user")
    item_comments = relationship("ItemComment", backref="user")
    roles = db.relationship('Role', secondary=roles_users,
                            backref=db.backref('users', lazy='dynamic'))
    role = db.Column(db.String(30), default="View")

    def __str__(self):
        return '<User id=%s email=%s>' % (self.id, self.email)


class ItemAudit(db.Model):
    """
    Meant to model an issue attached to a single item.
    """
    __tablename__ = "itemaudit"
    id = Column(Integer, primary_key=True)
    score = Column(Integer)
    issue = Column(String(512))
    notes = Column(String(512))
    justified = Column(Boolean)
    justified_user_id = Column(Integer, ForeignKey("user.id"), nullable=True, index=True)
    justification = Column(String(512))
    justified_date = Column(DateTime(), default=datetime.datetime.utcnow, nullable=True)
    item_id = Column(Integer, ForeignKey("item.id"), nullable=False, index=True)
    auditor_setting_id = Column(Integer, ForeignKey("auditorsettings.id"), nullable=True, index=True)

    def __str__(self):
        return "Issue: [{issue}] Score: {score} Justified: {justified}\nNotes: {notes}\n".format(
            issue=self.issue,
            score=self.score,
            justified=self.justified,
            notes=self.notes
        )

    def __repr__(self):
        return self.__str__()


class AuditorSettings(db.Model):
    """
    This table contains auditor disable settings.
    """
    __tablename__ = "auditorsettings"
    id = Column(Integer, primary_key=True)
    disabled = Column(Boolean(), nullable=False)
    issue_text = Column(String(512), nullable=True)
    auditor_class = Column(String(128))
    issues = relationship("ItemAudit", backref="auditor_setting")
    tech_id = Column(Integer, ForeignKey("technology.id"), index=True)
    account_id = Column(Integer, ForeignKey("account.id"), index=True)
    unique_const = UniqueConstraint('account_id', 'issue_text', 'tech_id')


class Item(db.Model):
    """
    Meant to model a specific item, like an instance of a security group.
    """
    __tablename__ = "item"
    id = Column(Integer, primary_key=True)
    cloud = Column(String(32))  # AWS, Google, Other
    region = Column(String(32))
    name = Column(String(303), index=True)  # Max AWS name = 255 chars.  Add 48 chars for ' (sg-12345678901234567 in vpc-12345678901234567)'
    arn = Column(Text(), nullable=True, index=True, unique=True)
    latest_revision_complete_hash = Column(String(32), index=True)
    latest_revision_durable_hash = Column(String(32), index=True)
    tech_id = Column(Integer, ForeignKey("technology.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("account.id"), nullable=False, index=True)
    latest_revision_id = Column(Integer, nullable=True)
    comments = relationship("ItemComment", backref="revision", cascade="all, delete, delete-orphan", order_by="ItemComment.date_created")
    revisions = relationship("ItemRevision", backref="item", cascade="all, delete, delete-orphan", order_by="desc(ItemRevision.date_created)", lazy="dynamic")
    issues = relationship("ItemAudit", backref="item", cascade="all, delete, delete-orphan")
    cloudtrail_entries = relationship("CloudTrailEntry", backref="item", cascade="all, delete, delete-orphan", order_by="CloudTrailEntry.event_time")

    exceptions = relationship("ExceptionLogs", backref="item", cascade="all, delete, delete-orphan")


class ItemComment(db.Model):
    """
    The Web UI allows users to add comments to items.
    """
    __tablename__ = "itemcomment"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey('item.id'), nullable=False, index=True)
    date_created = Column(DateTime(), default=datetime.datetime.utcnow, nullable=False)
    text = Column(Unicode(1024))

    def __str__(self):
        return "User [{user}]({date}): {text}".format(
            user=self.user.email,
            date=str(self.date_created),
            text=self.text
        )

    def __repr__(self):
        return self.__str__()


class ItemRevision(db.Model):
    """
    Every new configuration for an item is saved in a new ItemRevision.
    """
    __tablename__ = "itemrevision"
    id = Column(Integer, primary_key=True)
    active = Column(Boolean())
    config = deferred(Column(JSON))
    date_created = Column(DateTime(), default=datetime.datetime.utcnow, nullable=False, index=True)
    date_last_ephemeral_change = Column(DateTime(), nullable=True, index=True)
    item_id = Column(Integer, ForeignKey("item.id"), nullable=False, index=True)
    comments = relationship("ItemRevisionComment", backref="revision", cascade="all, delete, delete-orphan", order_by="ItemRevisionComment.date_created")
    cloudtrail_entries = relationship("CloudTrailEntry", backref="revision", cascade="all, delete, delete-orphan", order_by="CloudTrailEntry.event_time")


class CloudTrailEntry(db.Model):
    """
    Bananapeel (the security_monkey rearchitecture) will use this table to
    correlate CloudTrail entries to item revisions.
    """
    __tablename__ = 'cloudtrail'
    id = Column(Integer, primary_key=True)
    event_id = Column(String(36), index=True, unique=True)
    request_id = Column(String(36), index=True)
    event_source = Column(String(64), nullable=False)
    event_name = Column(String(64), nullable=False)
    event_time = Column(DateTime(), default=datetime.datetime.utcnow, nullable=False, index=True)
    request_parameters = deferred(Column(JSON))
    responseElements = deferred(Column(JSON))
    source_ip = Column(String(45))
    user_agent = Column(String(300))
    full_entry = deferred(Column(JSON))
    user_identity = deferred(Column(JSON))
    user_identity_arn = Column(String(300), index=True)
    revision_id = Column(Integer, ForeignKey('itemrevision.id'), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey('item.id'), nullable=False, index=True)


class ItemRevisionComment(db.Model):
    """
    The Web UI allows users to add comments to revisions.
    """
    __tablename__ = "itemrevisioncomment"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False, index=True)
    revision_id = Column(Integer, ForeignKey('itemrevision.id'), nullable=False, index=True)
    date_created = Column(DateTime(), default=datetime.datetime.utcnow, nullable=False)
    text = Column(Unicode(1024))


class NetworkWhitelistEntry(db.Model):
    """
    This table contains user-entered CIDR's that security_monkey
    will not alert on.
    """
    __tablename__ = "networkwhitelist"
    id = Column(Integer, primary_key=True)
    name = Column(String(512))
    notes = Column(String(512))
    cidr = Column(CIDR)


class IgnoreListEntry(db.Model):
    """
    This table contains user-entered prefixes that security_monkey
    will ignore when slurping the AWS config.
    """
    __tablename__ = "ignorelist"
    id = Column(Integer, primary_key=True)
    prefix = Column(String(512))
    notes = Column(String(512))
    tech_id = Column(Integer, ForeignKey("technology.id"), nullable=False, index=True)


class ExceptionLogs(db.Model):
    """
    This table stores all exceptions that are encountered, and provides metadata and context
    around the exceptions.
    """
    __tablename__ = "exceptions"
    id = Column(BigInteger, primary_key=True)
    source = Column(String(256), nullable=False, index=True)
    occurred = Column(DateTime, default=datetime.datetime.utcnow(), nullable=False)
    ttl = Column(DateTime, default=(datetime.datetime.utcnow() + datetime.timedelta(days=10)), nullable=False)
    type = Column(String(256), nullable=False, index=True)
    message = Column(String(512))
    stacktrace = Column(Text)
    region = Column(String(32), nullable=True, index=True)

    tech_id = Column(Integer, ForeignKey("technology.id", ondelete="CASCADE"), index=True)
    item_id = Column(Integer, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    account_id = Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), index=True)


class Datastore(object):
    def __init__(self, debug=False):
        pass

    def ephemeral_paths_for_tech(self, tech=None):
        """
        Returns the ephemeral paths for each technology.
        Note: this data is also in the watcher for each technology.
        It is mirrored here simply to assist in the security_monkey rearchitecture.
        :param tech: str, name of technology
        :return: list of ephemeral paths
        """
        ephemeral_paths = {
            'redshift': [
                "RestoreStatus",
                "ClusterStatus",
                "ClusterParameterGroups$ParameterApplyStatus",
                "ClusterParameterGroups$ClusterParameterStatusList$ParameterApplyErrorDescription",
                "ClusterParameterGroups$ClusterParameterStatusList$ParameterApplyStatus",
                "ClusterRevisionNumber"
            ],
            'securitygroup': ["assigned_to"],
            'iamuser': [
                "user$password_last_used",
                "accesskeys$*$LastUsedDate",
                "accesskeys$*$Region",
                "accesskeys$*$ServiceName"
            ]
        }
        return ephemeral_paths.get(tech, [])

    def durable_hash(self, item, ephemeral_paths):
        """
        Remove all ephemeral paths from the item and return the hash of the new structure.

        :param item: dictionary, representing an item tracked in security_monkey
        :return: hash of the sorted json dump of the item with all ephemeral paths removed.
        """
        durable_item = deepcopy(item)
        for path in ephemeral_paths:
            try:
                dpath.util.delete(durable_item, path, separator='$')
            except PathNotFound:
                pass
        return self.hash_config(durable_item)

    def hash_config(self, config):
        """
        Finds the hash for a config.
        Calls sub_dict, which is a recursive method which sorts lists which may be buried in the structure.
        Dumps the config to json with sort_keys set.
        Grabs an MD5 hash.
        :param config: dict describing item
        :return: 32 character string (MD5 Hash)
        """
        item = sub_dict(config)
        item_str = json.dumps(item, sort_keys=True)
        item_hash = hashlib.md5(item_str)
        return item_hash.hexdigest()

    def get_all_ctype_filtered(self, tech=None, account=None, region=None, name=None, include_inactive=False):
        """
        Returns a list of Items joined with their most recent ItemRevision,
        potentially filtered by the criteria above.
        """
        item_map = {}
        query = Item.query
        if tech:
            query = query.join((Technology, Item.tech_id == Technology.id)).filter(Technology.name == tech)
        if account:
            query = query.join((Account, Item.account_id == Account.id)).filter(Account.name == account)

        filter_by = {'region': region, 'name': name}
        for k, v in filter_by.items():
            if not v:
                del filter_by[k]

        query = query.filter_by(**filter_by)

        attempt = 1
        while True:
            try:
                items = query.all()
                break
            except Exception as e:
                app.logger.warn("Database Exception in Datastore::get_all_ctype_filtered. Sleeping for a few seconds. Attempt {}.".format(attempt))
                app.logger.debug("Exception: {}".format(e))
                import time
                time.sleep(5)
                attempt = attempt + 1
                if attempt > 5:
                    raise Exception("Too many retries for database connections.")

        for item in items:
            if not item.latest_revision_id:
                app.logger.debug("There are no itemrevisions for this item: {}".format(item.id))
                continue
            most_recent = ItemRevision.query.get(item.latest_revision_id)
            if not most_recent.active and not include_inactive:
                continue
            item_map[item] = most_recent

        return item_map

    def get(self, ctype, region, account, name):
        """
        Returns a list of all revisions for the given item.
        """
        item = self._get_item(ctype, region, account, name)
        return item.revisions

    def get_audit_issues(self, ctype, region, account, name):
        """
        Returns a list of ItemAudit objects associated with a given Item.
        """
        item = self._get_item(ctype, region, account, name)
        return item.issues

    def store(self, ctype, region, account, name, active_flag, config, arn=None, new_issues=[], ephemeral=False):
        """
        Saves an itemrevision.  Create the item if it does not already exist.
        """
        item = self._get_item(ctype, region, account, name)

        if arn:
            item.arn = arn

        item.latest_revision_complete_hash = self.hash_config(config)
        item.latest_revision_durable_hash = self.durable_hash(
            config,
            self.ephemeral_paths_for_tech(tech=ctype))

        if ephemeral:
            item_revision = item.revisions.first()
            item_revision.config = config
            item_revision.date_last_ephemeral_change = datetime.datetime.utcnow()
        else:
            item_revision = ItemRevision(active=active_flag, config=config)
            item.revisions.append(item_revision)

        # Add new issues
        for new_issue in new_issues:
            nk = "{}/{}".format(new_issue.issue, new_issue.notes)
            if nk not in ["{}/{}".format(old_issue.issue, old_issue.notes) for old_issue in item.issues]:
                item.issues.append(new_issue)
                db.session.add(new_issue)

        # Delete old issues
        for old_issue in item.issues:
            ok = "{}/{}".format(old_issue.issue, old_issue.notes)
            if ok not in ["{}/{}".format(new_issue.issue, new_issue.notes) for new_issue in new_issues]:
                db.session.delete(old_issue)

        db.session.add(item)
        db.session.add(item_revision)
        db.session.commit()

        self._set_latest_revision(item)

    def _set_latest_revision(self, item):
        latest_revision = item.revisions.first()
        item.latest_revision_id = latest_revision.id
        db.session.add(item)
        db.session.commit()
        #db.session.close()

    def _get_item(self, technology, region, account, name):
        """
        Returns the first item with matching parameters.
        Creates item if it doesn't exist.
        """
        account_result = Account.query.filter(Account.name == account).first()
        if not account_result:
            raise Exception("Account with name [{}] not found.".format(account))

        item = Item.query.join((Technology, Item.tech_id == Technology.id)) \
            .join((Account, Item.account_id == Account.id)) \
            .filter(Technology.name == technology) \
            .filter(Account.name == account) \
            .filter(Item.region == region) \
            .filter(Item.name == name) \
            .all()

        if len(item) > 1:
            # DB needs to be cleaned up and a bug needs to be found if this ever happens.
            raise Exception("Found multiple items for tech: {} region: {} account: {} and name: {}"
                            .format(technology, region, account, name))
        if len(item) == 1:
            item = item[0]
        else:
            item = None

        if not item:
            technology_result = Technology.query.filter(Technology.name == technology).first()
            if not technology_result:
                technology_result = Technology(name=technology)
                db.session.add(technology_result)
                db.session.commit()
                #db.session.close()
                app.logger.info("Creating a new Technology: {} - ID: {}"
                                .format(technology, technology_result.id))
            item = Item(tech_id=technology_result.id, region=region, account_id=account_result.id, name=name)
        return item


def store_exception(source, location, exception, ttl=None):
    """
    Method to store exceptions in the database.
    :param source:
    :param location:
    :param exception:
    :param ttl:
    :return:
    """
    try:
        app.logger.debug("Logging exception from {} with location: {} to the database.".format(source, location))
        message = str(exception)[:512]

        exception_entry = ExceptionLogs(source=source, ttl=ttl, type=type(exception).__name__,
                                        message=message, stacktrace=traceback.format_exc())
        if location:
            if len(location) == 4:
                item = Item.query.filter(Item.name == location[3]).first()
                if item:
                    exception_entry.item_id = item.id

            if len(location) >= 3:
                exception_entry.region = location[2]

            if len(location) >= 2:
                account = Account.query.filter(Account.name == location[1]).one()
                if account:
                    exception_entry.account_id = account.id

            technology = Technology.query.filter(Technology.name == location[0]).one()
            if technology:
                exception_entry.tech_id = technology.id

        db.session.add(exception_entry)
        db.session.commit()
        app.logger.debug("Completed logging exception to database.")

    except Exception as e:
        app.logger.error("Encountered exception while logging exception to database:")
        app.logger.exception(e)


def clear_old_exceptions():
    exc_list = ExceptionLogs.query.filter(ExceptionLogs.ttl <= datetime.datetime.utcnow()).all()

    for exc in exc_list:
        db.session.delete(exc)

    db.session.commit()
