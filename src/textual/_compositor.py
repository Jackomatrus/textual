"""

The compositor handles combining widgets in to a single screen (i.e. compositing).

It also stores the results of that process, so that Textual knows the widgets on
the screen and their locations. The compositor uses this information to answer
queries regarding the widget under an offset, or the style under an offset.

Additionally, the compositor can render portions of the screen which may have updated,
without having to render the entire screen.

"""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Callable, Iterable, NamedTuple, cast

import rich.repr
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.control import Control
from rich.segment import Segment
from rich.style import Style

from . import errors
from ._cells import cell_len
from ._context import visible_screen_stack
from ._loop import loop_last
from .geometry import NULL_OFFSET, Offset, Region, Size
from .strip import Strip, StripRenderable

if TYPE_CHECKING:
    from typing_extensions import TypeAlias

    from .screen import Screen
    from .widget import Widget


class ReflowResult(NamedTuple):
    """The result of a reflow operation. Describes the chances to widgets."""

    hidden: set[Widget]  # Widgets that are hidden
    shown: set[Widget]  # Widgets that are shown
    resized: set[Widget]  # Widgets that have been resized


class MapGeometry(NamedTuple):
    """Defines the absolute location of a Widget."""

    region: Region
    """The (screen) region occupied by the widget."""
    order: tuple[tuple[int, int, int], ...]
    """Tuple of tuples defining the painting order of the widget.

    Each successive triple represents painting order information with regards to
    ancestors in the DOM hierarchy and the last triple provides painting order
    information for this specific widget.
    """
    clip: Region
    """A region to clip the widget by (if a Widget is within a container)."""
    virtual_size: Size
    """The virtual size (scrollable region) of a widget if it is a container."""
    container_size: Size
    """The container size (area not occupied by scrollbars)."""
    virtual_region: Region
    """The region relative to the container (but not necessarily visible)."""

    @property
    def visible_region(self) -> Region:
        """The Widget region after clipping."""
        return self.clip.intersection(self.region)


# Maps a widget on to its geometry (information that describes its position in the composition)
CompositorMap: TypeAlias = "dict[Widget, MapGeometry]"


@rich.repr.auto(angular=True)
class LayoutUpdate:
    """A renderable containing the result of a render for a given region."""

    def __init__(self, strips: list[Strip], region: Region) -> None:
        self.strips = strips
        self.region = region

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        x = self.region.x
        new_line = Segment.line()
        move_to = Control.move_to
        for last, (y, line) in loop_last(enumerate(self.strips, self.region.y)):
            yield move_to(x, y)
            yield from line
            if not last:
                yield new_line

    def __rich_repr__(self) -> rich.repr.Result:
        yield self.region


@rich.repr.auto(angular=True)
class ChopsUpdate:
    """A renderable that applies updated spans to the screen."""

    def __init__(
        self,
        chops: list[dict[int, Strip | None]],
        spans: list[tuple[int, int, int]],
        chop_ends: list[list[int]],
    ) -> None:
        """A renderable which updates chops (fragments of lines).

        Args:
            chops: A mapping of offsets to list of segments, per line.
            crop: Region to restrict update to.
            chop_ends: A list of the end offsets for each line
        """
        self.chops = chops
        self.spans = spans
        self.chop_ends = chop_ends

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        move_to = Control.move_to
        new_line = Segment.line()
        chops = self.chops
        chop_ends = self.chop_ends
        last_y = self.spans[-1][0]

        _cell_len = cell_len
        for y, x1, x2 in self.spans:
            line = chops[y]
            ends = chop_ends[y]
            for end, (x, strip) in zip(ends, line.items()):
                # TODO: crop to x extents
                if strip is None:
                    continue

                if x > x2 or end <= x1:
                    continue

                if x2 > x >= x1 and end <= x2:
                    yield move_to(x, y)
                    yield from strip
                    continue

                iter_segments = iter(strip)
                if x < x1:
                    for segment in iter_segments:
                        next_x = x + _cell_len(segment.text)
                        if next_x > x1:
                            yield move_to(x, y)
                            yield segment
                            break
                        x = next_x
                else:
                    yield move_to(x, y)
                if end <= x2:
                    yield from iter_segments
                else:
                    for segment in iter_segments:
                        if x >= x2:
                            break
                        yield segment
                        x += _cell_len(segment.text)

            if y != last_y:
                yield new_line

    def __rich_repr__(self) -> rich.repr.Result:
        yield from ()


@rich.repr.auto(angular=True)
class Compositor:
    """Responsible for storing information regarding the relative positions of Widgets and rendering them."""

    def __init__(self) -> None:
        # A mapping of Widget on to its "render location" (absolute position / depth)

        self._full_map: CompositorMap = {}
        self._full_map_invalidated = True
        self._visible_map: CompositorMap | None = None
        self._layers: list[tuple[Widget, MapGeometry]] | None = None

        # All widgets considered in the arrangement
        # Note this may be a superset of self.full_map.keys() as some widgets may be invisible for various reasons
        self.widgets: set[Widget] = set()

        # Mapping of visible widgets on to their region, and clip region
        self._visible_widgets: dict[Widget, tuple[Region, Region]] | None = None

        # The top level widget
        self.root: Widget | None = None

        # Dimensions of the arrangement
        self.size = Size(0, 0)

        # The points in each line where the line bisects the left and right edges of the widget
        self._cuts: list[list[int]] | None = None

        # Regions that require an update
        self._dirty_regions: set[Region] = set()

        # Mapping of line numbers on to lists of widget and regions
        self._layers_visible: list[list[tuple[Widget, Region, Region]]] | None = None

    @classmethod
    def _regions_to_spans(
        cls, regions: Iterable[Region]
    ) -> Iterable[tuple[int, int, int]]:
        """Converts the regions to horizontal spans. Spans will be combined if they overlap
        or are contiguous to produce optimal non-overlapping spans.

        Args:
            regions: An iterable of Regions.

        Returns:
            Yields tuples of (Y, X1, X2).
        """
        inline_ranges: dict[int, list[tuple[int, int]]] = {}
        setdefault = inline_ranges.setdefault
        for region_x, region_y, width, height in regions:
            span = (region_x, region_x + width)
            for y in range(region_y, region_y + height):
                setdefault(y, []).append(span)

        slice_remaining = slice(1, None)
        for y, ranges in sorted(inline_ranges.items()):
            if len(ranges) == 1:
                # Special case of 1 span
                yield (y, *ranges[0])
            else:
                ranges.sort()
                x1, x2 = ranges[0]
                for next_x1, next_x2 in ranges[slice_remaining]:
                    if next_x1 <= x2:
                        if next_x2 > x2:
                            x2 = next_x2
                    else:
                        yield (y, x1, x2)
                        x1 = next_x1
                        x2 = next_x2
                yield (y, x1, x2)

    def __rich_repr__(self) -> rich.repr.Result:
        yield "size", self.size
        yield "widgets", self.widgets

    def reflow(self, parent: Widget, size: Size) -> ReflowResult:
        """Reflow (layout) widget and its children.

        Args:
            parent: The root widget.
            size: Size of the area to be filled.

        Returns:
            Hidden, shown, and resized widgets.
        """
        self._cuts = None
        self._layers = None
        self._layers_visible = None
        self._visible_widgets = None
        self._visible_map = None
        self.root = parent
        self.size = size

        # Keep a copy of the old map because we're going to compare it with the update
        old_map = self._full_map
        old_widgets = old_map.keys()

        map, widgets = self._arrange_root(parent, size)

        new_widgets = map.keys()

        # Replace map and widgets
        self._full_map = map
        self.widgets = widgets

        # Contains widgets + geometry for every widget that changed (added, removed, or updated)
        changes = map.items() ^ old_map.items()

        # Widgets in both new and old
        common_widgets = old_widgets & new_widgets

        # Mark dirty regions.
        screen_region = size.region
        if screen_region not in self._dirty_regions:
            regions = {
                region
                for region in (
                    map_geometry.clip.intersection(map_geometry.region)
                    for _, map_geometry in changes
                )
                if region
            }
            self._dirty_regions.update(regions)

        resized_widgets = {
            widget
            for widget, (region, *_) in changes
            if (widget in common_widgets and old_map[widget].region[2:] != region[2:])
        }
        # Newly visible widgets
        shown_widgets = new_widgets - old_widgets
        # Newly hidden widgets
        hidden_widgets = self.widgets - widgets
        return ReflowResult(
            hidden=hidden_widgets,
            shown=shown_widgets,
            resized=resized_widgets,
        )

    def reflow_visible(self, parent: Widget, size: Size) -> set[Widget]:
        """Reflow only the visible children.

        This is a fast-path for scrolling.

        Args:
            parent: The root widget.
            size: Size of the area to be filled.

        Returns:
            Set of widgets that were exposed by the scroll.

        """
        self._cuts = None
        self._layers = None
        self._layers_visible = None
        self._visible_widgets = None
        self._full_map_invalidated = True
        self.root = parent
        self.size = size

        # Keep a copy of the old map because we're going to compare it with the update
        old_map = (
            self._visible_map if self._visible_map is not None else self._full_map or {}
        )
        map, widgets = self._arrange_root(parent, size, visible_only=True)

        # Replace map and widgets
        self._visible_map = map
        self.widgets = widgets

        exposed_widgets = map.keys() - old_map.keys()

        # Contains widgets + geometry for every widget that changed (added, removed, or updated)
        changes = map.items() ^ old_map.items()

        # Mark dirty regions.
        screen_region = size.region
        if screen_region not in self._dirty_regions:
            regions = {
                region
                for region in (
                    map_geometry.clip.intersection(map_geometry.region)
                    for _, map_geometry in changes
                )
                if region
            }
            self._dirty_regions.update(regions)

        return exposed_widgets

    @property
    def full_map(self) -> CompositorMap:
        """Lazily built compositor map that covers all widgets."""

        if self.root is None:
            return {}
        if self._full_map_invalidated:
            self._full_map_invalidated = False
            map, _widgets = self._arrange_root(self.root, self.size, visible_only=False)
            self._full_map = map
            self._visible_widgets = None
            self._visible_map = None

        return self._full_map

    @property
    def visible_widgets(self) -> dict[Widget, tuple[Region, Region]]:
        """Get a mapping of widgets on to region and clip.

        Returns:
            Visible widget mapping.
        """

        if self._visible_widgets is None:
            map = (
                self._visible_map
                if self._visible_map is not None
                else (self._full_map or {})
            )
            screen = self.size.region
            in_screen = screen.overlaps
            overlaps = Region.overlaps

            # Widgets and regions in render order
            visible_widgets = [
                (order, widget, region, clip)
                for widget, (region, order, clip, _, _, _) in map.items()
                if in_screen(region) and overlaps(clip, region)
            ]
            visible_widgets.sort(key=itemgetter(0), reverse=True)
            self._visible_widgets = {
                widget: (region, clip) for _, widget, region, clip in visible_widgets
            }
        return self._visible_widgets

    def _arrange_root(
        self, root: Widget, size: Size, visible_only: bool = True
    ) -> tuple[CompositorMap, set[Widget]]:
        """Arrange a widget's children based on its layout attribute.

        Args:
            root: Top level widget.

        Returns:
            Compositor map and set of widgets.
        """

        ORIGIN = NULL_OFFSET

        map: CompositorMap = {}
        widgets: set[Widget] = set()
        add_new_widget = widgets.add
        layer_order: int = 0

        def add_widget(
            widget: Widget,
            virtual_region: Region,
            region: Region,
            order: tuple[tuple[int, int, int], ...],
            layer_order: int,
            clip: Region,
            visible: bool,
            _MapGeometry: type[MapGeometry] = MapGeometry,
        ) -> None:
            """Called recursively to place a widget and its children in the map.

            Args:
                widget: The widget to add.
                virtual_region: The Widget region relative to it's container.
                region: The region the widget will occupy.
                order: Painting order information.
                layer_order: The order of the widget in its layer.
                clip: The clipping region (i.e. the viewport which contains it).
                visible: Whether the widget should be visible by default.
                    This may be overridden by the CSS rule `visibility`.
            """
            visibility = widget.styles.get_rule("visibility")
            if visibility is not None:
                visible = visibility == "visible"

            if visible:
                add_new_widget(widget)
            styles_offset = widget.styles.offset
            layout_offset = (
                styles_offset.resolve(region.size, clip.size)
                if styles_offset
                else ORIGIN
            )

            # Container region is minus border
            container_region = region.shrink(widget.styles.gutter).translate(
                layout_offset
            )
            container_size = container_region.size

            # Widgets with scrollbars (containers or scroll view) require additional processing
            if widget.is_scrollable:
                # The region that contains the content (container region minus scrollbars)
                child_region = widget._get_scrollable_region(container_region)

                # Adjust the clip region accordingly
                sub_clip = clip.intersection(child_region)

                # The region covered by children relative to parent widget
                total_region = child_region.reset_offset

                if widget.is_container:
                    # Arrange the layout
                    arrange_result = widget._arrange(child_region.size)
                    arranged_widgets = arrange_result.widgets
                    widgets.update(arranged_widgets)

                    if visible_only:
                        placements = arrange_result.get_visible_placements(
                            container_size.region + widget.scroll_offset
                        )
                    else:
                        placements = arrange_result.placements
                    total_region = total_region.union(arrange_result.total_region)

                    # An offset added to all placements
                    placement_offset = container_region.offset
                    placement_scroll_offset = placement_offset - widget.scroll_offset

                    _layers = widget.layers
                    layers_to_index = {
                        layer_name: index for index, layer_name in enumerate(_layers)
                    }
                    get_layer_index = layers_to_index.get

                    scroll_spacing = arrange_result.scroll_spacing

                    # Add all the widgets
                    for sub_region, margin, sub_widget, z, fixed in reversed(
                        placements
                    ):
                        layer_index = get_layer_index(sub_widget.layer, 0)
                        # Combine regions with children to calculate the "virtual size"
                        if fixed:
                            widget_region = sub_region + placement_offset
                        else:
                            total_region = total_region.union(
                                sub_region.grow(
                                    margin if layer_index else margin + scroll_spacing
                                )
                            )
                            widget_region = sub_region + placement_scroll_offset

                        widget_order = order + ((layer_index, z, layer_order),)

                        add_widget(
                            sub_widget,
                            sub_region,
                            widget_region,
                            widget_order,
                            layer_order,
                            sub_clip,
                            visible,
                        )

                        layer_order -= 1

                if visible:
                    # Add any scrollbars
                    if any(widget.scrollbars_enabled):
                        for chrome_widget, chrome_region in widget._arrange_scrollbars(
                            container_region
                        ):
                            map[chrome_widget] = _MapGeometry(
                                chrome_region,
                                order,
                                clip,
                                container_size,
                                container_size,
                                chrome_region,
                            )

                    map[widget] = _MapGeometry(
                        region + layout_offset,
                        order,
                        clip,
                        total_region.size,
                        container_size,
                        virtual_region,
                    )

            elif visible:
                # Add the widget to the map
                map[widget] = _MapGeometry(
                    region + layout_offset,
                    order,
                    clip,
                    region.size,
                    container_size,
                    virtual_region,
                )

        # Add top level (root) widget
        add_widget(
            root,
            size.region,
            size.region,
            ((0, 0, 0),),
            layer_order,
            size.region,
            True,
        )
        return map, widgets

    @property
    def layers(self) -> list[tuple[Widget, MapGeometry]]:
        """Get widgets and geometry in layer order."""
        map = self._visible_map if self._visible_map is not None else self._full_map
        if self._layers is None:
            self._layers = sorted(
                map.items(), key=lambda item: item[1].order, reverse=True
            )
        return self._layers

    @property
    def layers_visible(self) -> list[list[tuple[Widget, Region, Region]]]:
        """Visible widgets and regions in layers order."""

        if self._layers_visible is None:
            layers_visible: list[list[tuple[Widget, Region, Region]]]
            layers_visible = [[] for y in range(self.size.height)]
            layers_visible_appends = [layer.append for layer in layers_visible]
            intersection = Region.intersection
            _range = range
            for widget, (region, clip) in self.visible_widgets.items():
                cropped_region = intersection(region, clip)
                _x, region_y, _width, region_height = cropped_region
                if region_height:
                    widget_location = (widget, cropped_region, region)
                    for y in _range(region_y, region_y + region_height):
                        layers_visible_appends[y](widget_location)
            self._layers_visible = layers_visible
        return self._layers_visible

    def get_offset(self, widget: Widget) -> Offset:
        """Get the offset of a widget."""
        try:
            if self._visible_map is not None:
                try:
                    return self._visible_map[widget].region.offset
                except KeyError:
                    pass
            return self.full_map[widget].region.offset
        except KeyError:
            raise errors.NoWidget("Widget is not in layout")

    def get_widget_at(self, x: int, y: int) -> tuple[Widget, Region]:
        """Get the widget under a given coordinate.

        Args:
            x: X Coordinate.
            y: Y Coordinate.

        Raises:
            errors.NoWidget: If there is not widget underneath (x, y).

        Returns:
            A tuple of the widget and its region.
        """

        contains = Region.contains
        if len(self.layers_visible) > y >= 0:
            for widget, cropped_region, region in self.layers_visible[y]:
                if contains(cropped_region, x, y) and widget.visible:
                    return widget, region
        raise errors.NoWidget(f"No widget under screen coordinate ({x}, {y})")

    def get_widgets_at(self, x: int, y: int) -> Iterable[tuple[Widget, Region]]:
        """Get all widgets under a given coordinate.

        Args:
            x: X coordinate.
            y: Y coordinate.

        Returns:
            Sequence of (WIDGET, REGION) tuples.
        """
        contains = Region.contains
        for widget, cropped_region, region in self.layers_visible[y]:
            if contains(cropped_region, x, y) and widget.visible:
                yield widget, region

    def get_style_at(self, x: int, y: int) -> Style:
        """Get the Style at the given cell or Style.null()

        Args:
            x: X position within the Layout
            y: Y position within the Layout

        Returns:
            The Style at the cell (x, y) within the Layout
        """
        try:
            widget, region = self.get_widget_at(x, y)
        except errors.NoWidget:
            return Style.null()
        if widget not in self.visible_widgets:
            return Style.null()

        x -= region.x
        y -= region.y

        visible_screen_stack.set(widget.app._background_screens)
        lines = widget.render_lines(Region(0, y, region.width, 1))

        if not lines:
            return Style.null()
        end = 0
        for segment in lines[0]:
            end += segment.cell_length
            if x < end:
                return segment.style or Style.null()
        return Style.null()

    def find_widget(self, widget: Widget) -> MapGeometry:
        """Get information regarding the relative position of a widget in the Compositor.

        Args:
            widget: The Widget in this layout you wish to know the Region of.

        Raises:
            NoWidget: If the Widget is not contained in this Layout.

        Returns:
            Widget's composition information.

        """
        if self.root is None:
            raise errors.NoWidget("Widget is not in layout")
        try:
            if self._full_map is not None:
                try:
                    return self._full_map[widget]
                except KeyError:
                    pass
            if self._visible_map is not None:
                try:
                    return self._visible_map[widget]
                except KeyError:
                    pass
            region = self.full_map[widget]
        except KeyError:
            raise errors.NoWidget("Widget is not in layout")
        else:
            return region

    @property
    def cuts(self) -> list[list[int]]:
        """Get vertical cuts.

        A cut is every point on a line where a widget starts or ends.

        Returns:
            A list of cuts for every line.
        """
        if self._cuts is not None:
            return self._cuts

        width, height = self.size
        screen_region = self.size.region
        cuts = [[0, width] for _ in range(height)]

        intersection = Region.intersection
        extend = list.extend

        for region, clip in self.visible_widgets.values():
            region = intersection(region, clip)
            if region and (region in screen_region):
                x, y, region_width, region_height = region
                region_cuts = (x, x + region_width)
                for cut in cuts[y : y + region_height]:
                    extend(cut, region_cuts)

        # Sort the cuts for each line
        self._cuts = [sorted(set(line_cuts)) for line_cuts in cuts]

        return self._cuts

    def _get_renders(
        self, crop: Region | None = None
    ) -> Iterable[tuple[Region, Region, list[Strip]]]:
        """Get rendered widgets (lists of segments) in the composition.

        Args:
            crop: Region to crop to, or `None` for entire screen.

        Returns:
            An iterable of <region>, <clip region>, and <strips>
        """
        # If a renderable throws an error while rendering, the user likely doesn't care about the traceback
        # up to this point.
        _rich_traceback_guard = True

        _Region = Region

        visible_widgets = self.visible_widgets

        if crop:
            crop_overlaps = crop.overlaps
            widget_regions = [
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
                if crop_overlaps(clip) and widget.styles.opacity > 0
            ]
        else:
            widget_regions = [
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
                if widget.styles.opacity > 0
            ]

        intersection = _Region.intersection
        contains_region = _Region.contains_region

        for widget, region, clip in widget_regions:
            if contains_region(clip, region):
                yield region, clip, widget.render_lines(
                    _Region(0, 0, region.width, region.height)
                )
            else:
                clipped_region = intersection(region, clip)
                if not clipped_region:
                    continue
                new_x, new_y, new_width, new_height = clipped_region
                delta_x = new_x - region.x
                delta_y = new_y - region.y
                yield region, clip, widget.render_lines(
                    _Region(delta_x, delta_y, new_width, new_height)
                )

    def render_update(
        self, full: bool = False, screen_stack: list[Screen] | None = None
    ) -> RenderableType | None:
        """Render an update renderable.

        Args:
            full: Enable full update, or `False` for a partial update.

        Returns:
            A renderable for the update, or `None` if no update was required.
        """

        visible_screen_stack.set([] if screen_stack is None else screen_stack)
        screen_region = self.size.region
        if full or screen_region in self._dirty_regions:
            return self.render_full_update()
        else:
            return self.render_partial_update()

    def render_full_update(self) -> LayoutUpdate:
        """Render a full update.

        Returns:
            A LayoutUpdate renderable.
        """
        screen_region = self.size.region
        self._dirty_regions.clear()
        crop = screen_region
        chops = self._render_chops(crop, lambda y: True)
        render_strips = [Strip.join(chop.values()) for chop in chops]
        return LayoutUpdate(render_strips, screen_region)

    def render_partial_update(self) -> ChopsUpdate | None:
        """Render a partial update.

        Returns:
            A ChopsUpdate if there is anything to update, otherwise `None`.

        """
        screen_region = self.size.region
        update_regions = self._dirty_regions.copy()
        self._dirty_regions.clear()
        if update_regions:
            # Create a crop region that surrounds all updates.
            crop = Region.from_union(update_regions).intersection(screen_region)
            spans = list(self._regions_to_spans(update_regions))
            is_rendered_line = {y for y, _, _ in spans}.__contains__
        else:
            return None
        chops = self._render_chops(crop, is_rendered_line)
        chop_ends = [cut_set[1:] for cut_set in self.cuts]
        return ChopsUpdate(chops, spans, chop_ends)

    def render_strips(self) -> list[Strip]:
        """Render to a list of strips.

        Returns:
            A list of strips with the screen content.
        """
        chops = self._render_chops(self.size.region, lambda y: True)
        render_strips = [Strip.join(chop.values()) for chop in chops]
        return render_strips

    def _render_chops(
        self,
        crop: Region,
        is_rendered_line: Callable[[int], bool],
    ) -> list[dict[int, Strip | None]]:
        """Render update 'chops'.

        Args:
            crop: Region to crop to.
            is_rendered_line: Callable to check if line should be rendered.

        Returns:
            Chops structure.
        """
        cuts = self.cuts
        fromkeys = cast("Callable[[list[int]], dict[int, Strip | None]]", dict.fromkeys)
        chops: list[dict[int, Strip | None]]
        chops = [fromkeys(cut_set[:-1]) for cut_set in cuts]

        cut_strips: Iterable[Strip]

        # Go through all the renders in reverse order and fill buckets with no render
        renders = self._get_renders(crop)
        intersection = Region.intersection

        for region, clip, strips in renders:
            render_region = intersection(region, clip)

            for y, strip in zip(render_region.line_range, strips):
                if not is_rendered_line(y):
                    continue

                chops_line = chops[y]

                first_cut, last_cut = render_region.column_span
                cuts_line = cuts[y]
                final_cuts = [
                    cut for cut in cuts_line if (last_cut >= cut >= first_cut)
                ]
                if len(final_cuts) <= 2:
                    # Two cuts, which means the entire line
                    cut_strips = [strip]
                else:
                    render_x = render_region.x
                    relative_cuts = [cut - render_x for cut in final_cuts[1:]]
                    cut_strips = strip.divide(relative_cuts)

                # Since we are painting front to back, the first segments for a cut "wins"
                for cut, strip in zip(final_cuts, cut_strips):
                    if chops_line[cut] is None:
                        chops_line[cut] = strip

        return chops

    def __rich__(self) -> StripRenderable:
        return StripRenderable(self.render_strips())

    def update_widgets(self, widgets: set[Widget]) -> None:
        """Update a given widget in the composition.

        Args:
            widgets: Set of Widgets to update.

        """
        # If there are any *new* widgets we need to invalidate the full map
        if not self._full_map_invalidated and not widgets.issubset(
            self.visible_widgets.keys()
        ):
            self._full_map_invalidated = True

        regions: list[Region] = []
        add_region = regions.append
        get_widget = self.visible_widgets.__getitem__
        for widget in self.visible_widgets.keys() & widgets:
            region, clip = get_widget(widget)
            offset = region.offset
            intersection = clip.intersection
            for dirty_region in widget._exchange_repaint_regions():
                update_region = intersection(dirty_region.translate(offset))
                if update_region:
                    add_region(update_region)

        self._dirty_regions.update(regions)
