
import os

from sqlalchemy.orm.exc import NoResultFound

from delivery.models.db_models import StagingOrder

class DatabaseBasedStagingRepository(object):

    def __init__(self, session_factory):
        self.session = session_factory()

    def get_staging_order_by_source(self, source):
        return self.session.query(StagingOrder).filter(StagingOrder.source == source).all()

    def get_staging_order_by_id(self, identifier, custom_session=None):
        if custom_session:
            session = custom_session
        else:
            session = self.session
        try:
            return session.query(StagingOrder).filter(StagingOrder.id == identifier).one()
        except NoResultFound:
            return None

    def create_staging_order(self, source, status, staging_target_dir):

        order = StagingOrder(source=source, status=status)
        self.session.add(order)

        self.session.commit()

        staging_target = os.path.join(staging_target_dir,
                                      "{}_{}".format(order.id,
                                                     os.path.basename(order.source)))

        order.staging_target = staging_target
        self.session.commit()

        return order
