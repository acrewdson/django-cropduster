#Place in cropduster/management/commands/regenerate_thumbs.py.
# Afterwards, the command can be run using:
# manage.py regenerate_thumbs
#
# Search for 'changeme' for lines that should be modified

import sys
import os
import logging
import inspect
import traceback
from collections import namedtuple
from optparse import make_option

from django.db.models.base import ModelBase
from django.core.management.base import BaseCommand, CommandError

from cropduster.models import Image as CropDusterImage,CropDusterField as CDF
from cropduster.utils import create_cropped_image, rescale
import apputils
import Image

def to_CE(f, *args, **kwargs):
    """
    Simply re-raises any error as a CommandError.
    
    @param f: function to call
    @type  f: callable(f) 

    @return: f(*args, **kwargs)
    @rtype: object
    """
    try:
        return f(*args, **kwargs)
    except Exception, e:
        sys.stderr.write(traceback.format_exc(e))
        raise CommandError('Error: %s(%s)' % (type(e), e))
 
class PrettyError(object):
    def __init__(self, msg):
        self.msg = msg

    def __call__(self, f):
        def _f(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except CommandError:
                raise
            except Exception, e:
                raise CommandError(self.msg % dict(error=e))

        return _f

Size = namedtuple('Size', ('name', 'path', 'crop', 'width', 'height'))

class Command(BaseCommand):
    args = "app_name[:model[.field]][, ...]"
    help = "Regenerates cropduster thumbnails for an entire "\
           "app or specific model and/or field."

    option_list = BaseCommand.option_list + (
        make_option('--force',
                    action  = "store_true",
                    dest    = "force",
                    default = False,
                    help    = "Resizes all images regardless of whether or not"\
                              " they already exist."),

        make_option('--query_set',
                    dest    = "query_set",
                    default = "all()",
                    help    = "Queryset to use.  Default uses all models."),

        make_option('--stretch',
                    dest    = 'stretch',
                    action  = "store_true",
                    default = False,
                    help    = "Indicates whether to resize an image if size is larger"\
                              " than original.  Default is False."),

        make_option('--log_file',
                    dest='logfile',
                    default = 'regen_thumbs.out',
                    help="Location of the log file.  Default regen_thumbs.out"),

        make_option('--log_level',
                    dest='loglevel',
                    default = 'INFO',
                    help="One of ERROR, INFO, DEBUG.  Default is INFO")
    )
    
    IMG_TYPE_PARAMS = {
        'JPEG': dict(quality=95)
    }
    
    def get_queryset(self, model, query_str):
        """
        Gets the query set from the provided model based on the user's filters.

        @param model: Django Model to query
        @type  model: Class 
        
        @param query_str: Filter query to retrieve objects with
        @type  query_str: "filter string" 

        @return: QuerySet for the given model.
        @rtype:  <iterable of object>
        """
        query_str = 'model.objects.' + query_str.lstrip('.')
        return eval(query_str, dict(model=model))

    def resize_image(self, image, sizes, force):
        """
        Resizes an image to the provided set sizes.

        @param image: Opened original image
        @type  image: PIL.Image
        
        @param sizes: Set of sizes to create.
        @type  sizes: [Size1, ...]
        
        @param force: Whether or not to recreate a thumbnail if it already exists.
        @type  force: bool

        @return: 
        @rtype: 
        """
        img_params = (self.IMG_TYPE_PARAMS.get(image.format) or {}).copy()
        for size in sizes:
            
            logging.debug('Converting image to size `%s` (%s x %s)' % (size.name,
                                                                       size.width,
                                                                       size.height))
            # Do we need to recreate the file?
            if not force and os.path.isfile(size.path) and os.stat(size.path).st_size > 0:
                logging.debug(' - Image `%s` exists, skipping...' % size.name)
                continue

            folder, _basename = os.path.split(size.path)
            if not os.path.isdir(folder):
                logging.debug(' - Directory %s does not exist.  Creating...' % folder)
                os.makedirs(folder)

            try:
                # In place scaling, so we need to use a copy of the image.
                thumbnail = rescale(image.copy(),
                                    size.width,
                                    size.height,
                                    crop=size.crop)

                tmp_path = size.path + '.tmp'

                thumbnail.save(tmp_path, image.format, **img_params)

            # No idea what this can throw, so catch them all
            except Exception, e:
                logging.exception('Error saving thumbnail to %s' % tmp_path)
                resp = raw_input('Continue? [Y/n]: ')
                if resp.lower().strip() == 'n':
                    raise SystemExit('Exiting...')
                
            else:
                os.rename(tmp_path, size.path)
            
    def get_sizes(self, cd_image, stretch):
        """
        Extracts sizes for an image.

        @param cd_image: Cropduster image to use
        @type  cd_image: CropDusterImage

        @param stretch: Indicates whether or not we want to include sizes that 
                        would stretch the original image.
        @type  stretch: bool

        @return: Set of sizes to use
        @rtype:  Sizes
        """
        sizes = []
        orig_width, orig_height = cd_image.image.width, cd_image.image.height
        for size in cd_image.size_set.size_set.all():

            # Filter out thumbnail sizes which are larger than the original
            if stretch or (orig_width >= size.width and 
                           orig_height >= size.height):

                sizes.append( Size(size.slug,
                                   cd_image.thumbnail_path(size),
                                   size.auto_size,
                                   size.width,
                                   size.height) )
        return set(sizes)


    def setup_logging(self, options):
        """
        Sets up logging details.
        """
        logging.basicConfig(filename=options['logfile'],
                            level = getattr(logging, options['loglevel'].upper()),
                            format="%(asctime)s %(levelname)s %(message)s")

    @PrettyError("Failed to regenerate thumbs: %(error)s")
    def handle(self, *apps, **options):
        """
        Resolves out the models and images for regeneratating thumbnails and
        then resolves them.
        """
        
        self.setup_logging(options)

        # Figures out the models and cropduster fields on them
        for model, field_names in to_CE(apputils.resolve_apps, apps):

            logging.info("Processing model %s with fields %s" % (model, field_names))

            # Returns the queryset for each model
            query = self.get_queryset(model, options['query_set'])
            logging.info("Queryset return %i objects" % query.count())
            for obj in query:

                for field_name in field_names:

                    # Sanity check; we really should have a cropduster image here.
                    cd_image = getattr(obj, field_name)
                    if not (cd_image and isinstance(cd_image, CropDusterImage)):
                        continue

                    file_name = cd_image.image.path
                    logging.info("Processing image %s" % file_name)
                    try:
                        image = Image.open(file_name)
                    except IOError:
                        logging.warning('Could not open image %s' % file_name)
                        continue

                    sizes = self.get_sizes(cd_image, options['stretch'])
                    self.resize_image(image, sizes, options['force'])
