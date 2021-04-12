import h5py
import dask.distributed
from torch.utils.data import Dataset
from pathlib import Path

import pathml.core.masks
import pathml.core.tile
import pathml.core.tiles
import pathml.core.slide_backends
import pathml.core.h5path 
import pathml.preprocessing.pipeline


class SlideData:
    """
    Main class representing a slide and its annotations. 

    Args:
        filepath (str, optional): Path to slide file on disk.
        name (str, optional): name of slide. If ``None``, and a ``filepath`` is provided, name defaults to filepath.
        slide_backend (pathml.core.slide_backends.SlideBackend, optional): slide_backend object for interfacing with
            slide on disk. If ``None``, and a ``filepath`` is provided, defaults to
             :class:`~pathml.core.slide_backends.OpenSlideBackend`
        masks (pathml.core.masks.Masks, optional): object containing {key, mask} pairs
        tiles (pathml.core.tiles.Tiles, optional): object containing {coordinates, tile} pairs
        labels (collections.OrderedDict, optional): dictionary containing {key, label} pairs
    """
    def __init__(self, filepath=None, name=None, slide_backend=None, masks=None, tiles=None, labels=None, history=None):
        # check inputs
        assert masks is None or isinstance(masks, (pathml.core.masks.Masks, h5py._hl.group.Group)), \
            f"mask are of type {type(masks)} but must be type Masks or h5 group"
        assert labels is None or isinstance(labels, dict), \
            f"labels are of type {type(labels)} but must be of type dict. array-like labels should be stored in masks."
        assert tiles is None or isinstance(tiles, (pathml.core.tiles.Tiles, h5py._hl.group.Group)), \
            f"tiles are of type {type(tiles)} but must be of type pathml.core.tiles.Tiles"
        assert slide_backend is None or issubclass(slide_backend, pathml.core.slide_backends.SlideBackend), \
            f"slide_backend is of type {type(slide_backend)} but must be a subclass of pathml.core.slide_backends.SlideBackend"

        # load slide using OpenSlideBackend if path is provided and backend is not specified
        if filepath is not None:
            if slide_backend is None:
                slide_backend = pathml.core.slide_backends.OpenSlideBackend
            slide = slide_backend(filepath)
        else:
            slide = None

        # get name from filepath if no name is provided
        if name is None and filepath is not None:
            name = Path(filepath).stem

        self.slide = slide
        self.slide_backend = slide_backend
        self.name = name
        self.masks = masks
        self.tiles = tiles
        self.labels = labels
        self.history = history
        self.tile_dataset = None

    def __repr__(self): 
        out = f"{self.__class__.__name__}(name={self.name}, "
        out += f"slide = {repr(self.slide)}, "
        out += f"masks={repr(self.masks)}, "
        out += f"tiles={repr(self.tiles)}, "
        out += f"labels={repr(self.labels)}, "
        out += f"history={self.history})"
        return out 

    def run(self, pipeline, client=None, tile_size=3000, tile_stride=None, level=0, tile_pad=False,
            overwrite_existing_tiles=False):
        """
        Run a preprocessing pipeline on SlideData.
        Tiles are generated by calling self.generate_tiles() and pipeline is applied to each tile.

        Args:
            pipeline (pathml.preprocessing.pipeline.Pipeline): Preprocessing pipeline.
            client: dask.distributed client
            tile_size (int, optional): Size of each tile. Defaults to 3000px
            tile_stride (int, optional): Stride between tiles. If ``None``, uses ``tile_stride = tile_size``
                for non-overlapping tiles. Defaults to ``None``.
            level (int, optional): Level to extract tiles from. Defaults to ``None``.
            tile_pad (bool): How to handle chunks on the edges. If ``True``, these edge chunks will be zero-padded
                symmetrically and yielded with the other chunks. If ``False``, incomplete edge chunks will be ignored.
                Defaults to ``False``.
            overwrite_existing_tiles (bool): Whether to overwrite existing tiles. If ``False``, running a pipeline will
                fail if ``tiles is not None``. Defaults to ``False``.
        """
        assert isinstance(pipeline, pathml.preprocessing.pipeline.Pipeline), \
            f"pipeline is of type {type(pipeline)} but must be of type pathml.preprocessing.pipeline.Pipeline"
        assert self.slide is not None, "cannot run pipeline because self.slide is None"

        if self.tiles is None:
            self.tiles = pathml.core.tiles.Tiles()
        else:
            if overwrite_existing_tiles:
                self.tiles = pathml.core.tiles.Tiles()
            else:
                raise Exception("Slide already has tiles. Running the pipeline will overwrite the existing tiles."
                                "use overwrite_existing_tiles=True to force overwriting existing tiles.")

        if client is None:
            client = dask.distributed.Client()

        # map pipeline application onto each tile
        processed_tile_futures = []

        for tile in self.generate_tiles(level = level, shape = tile_size, stride = tile_stride, pad = tile_pad):
            f = client.submit(pipeline.apply, tile)
            processed_tile_futures.append(f)

        # as tiles are processed, add them to h5
        for future, tile in dask.distributed.as_completed(processed_tile_futures, with_results = True):
            self.tiles.add(tile)

        # after running preprocessing, create a pytorch dataset for the tiles
        self.tile_dataset = self._create_tile_dataset(self)

    @staticmethod
    def _create_tile_dataset(slidedata):
        # create a pytorch dataset for tiles, also with slide-level labels
        class TileDataset(Dataset):
            def __init__(self, slidedata):
                self.tiles = slidedata.tiles
                self.labels = slidedata.labels

            def __len__(self):
                return len(self.tiles)

            def __getitem__(self, ix):
                return self.tiles[ix], self.labels

        return TileDataset(slidedata)

    def generate_tiles(self, shape=3000, stride=None, pad=False, **kwargs):
        """
        Generator over Tile objects containing regions of the image.
        Calls ``generate_tiles()`` method of the backend.
        Tries to add the corresponding slide-level masks to each tile, if possible.
        Adds slide-level labels to each tile, if possible.

        Args:
            shape (int or tuple(int)): Size of each tile. May be a tuple of (height, width) or a single integer,
                in which case square tiles of that size are generated.
            stride (int): stride between chunks. If ``None``, uses ``stride = size`` for non-overlapping chunks.
                Defaults to ``None``.
            pad (bool): How to handle tiles on the edges. If ``True``, these edge tiles will be zero-padded
                and yielded with the other chunks. If ``False``, incomplete edge chunks will be ignored.
                Defaults to ``False``.
            **kwargs: Other arguments passed through to ``generate_tiles()`` method of the backend.

        Yields:
            pathml.core.tile.Tile: Extracted Tile object
        """
        for tile in self.slide.generate_tiles(shape, stride, pad, **kwargs):
            # add masks for tile, if possible
            # i.e. if the SlideData has a Masks object, and the tile has coordinates
            if self.masks is not None and tile.coords is not None:
                # masks not supported if pad=True
                # to implement, need to update Mask.slice to support slices that go beyond the full mask
                if not pad:
                    i, j = tile.coords
                    di, dj = tile.image.shape[0:2]
                    # add the Masks object for the masks corresponding to the tile
                    # this assumes that the tile didn't already have any masks
                    # this should work since the backend reads from image only
                    # adding safety check just in case to make sure we don't overwrite any existing mask
                    # if this assertion fails, we will need to rewrite this part
                    assert len(tile.masks) == 0, \
                        "tile yielded from backend already has mask. slide_data.generate_tiles is trying to overwrite it"

                    tile_slices = [slice(i, i + di), slice(j, j + dj)]
                    tile.masks = self.masks.slice(tile_slices)

            # add slide-level labels to each tile, if possible
            if self.labels is not None:
                tile.labels = self.labels

            # add slidetype to tile
            if tile.slidetype is None:
                tile.slidetype = type(self)

            yield tile

    def plot(self):
        raise NotImplementedError

    def write(self, path):
        """
        Write contents to disk in h5path format.

        Args:
            path (Union[str, bytes, os.PathLike]): path to file to be written
        """
        pathml.core.h5path.write_h5path(self, path)
