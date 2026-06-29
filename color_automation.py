#!/usr/bin/env python
import re

try:
    from photoshop import Session
except ImportError:
    # Photoshop COM bridge is only available on Windows with Photoshop installed.
    # The pure color helpers below remain usable (and testable) without it.
    Session = None

def hex_to_rgb(hex_color):
    """
    Convert a hex color string (e.g. "#RRGGBB" or "#RGB") to an (R, G, B) tuple.
    Supports both 6-digit (#RRGGBB) and 3-digit shorthand (#RGB).
    """
    hex_color = hex_color.lstrip('#')
    
    # Expand 3-digit shorthand to 6-digit (#FFF → #FFFFFF)
    if len(hex_color) == 3:
        hex_color = ''.join([c*2 for c in hex_color])
    
    if len(hex_color) != 6:
        raise ValueError(f"Hex color must be in format #RRGGBB or #RGB, got: #{hex_color}")
    
    red   = int(hex_color[0:2], 16)
    green = int(hex_color[2:4], 16)
    blue  = int(hex_color[4:6], 16)
    return red, green, blue

def is_valid_hex_color(hex_color):
    """
    Returns True if hex_color is a valid hex color string in the form "#RRGGBB"
    (case-insensitive) and is not "default". Otherwise returns False.
    Supports both 6-digit (#RRGGBB) and 3-digit shorthand (#RGB).
    """
    if not hex_color:
        return False
    
    hex_color = hex_color.strip().lower()
    
    if hex_color == "default":
        return False
    
    # Check for valid 6-digit hex: #RRGGBB
    if re.fullmatch(r"#([A-Fa-f0-9]{6})", hex_color):
        return True
    
    # Check for valid 3-digit hex shorthand: #RGB (will expand to #RRGGBB)
    if re.fullmatch(r"#([A-Fa-f0-9]{3})", hex_color):
        return True
    
    return False

def select_bg_color_layer(ps):
    """
    Selects the layer named "Bg_Color" in the active document.
    If it doesn't exist, creates one.
    Returns the layer.
    """
    doc = ps.app.activeDocument
    try:
        bg_layer = doc.ArtLayers["Bg_Color"]
    except Exception:
        ps.echo("Layer 'Bg_Color' not found. Creating a new one.")
        bg_layer = doc.artLayers.add()
        bg_layer.name = "Bg_Color"
    doc.activeLayer = bg_layer
    ps.echo("The layer 'Bg_Color' is now the active layer.")
    return bg_layer

def fill_active_selection_with_color(ps, desired_hex, fill_opacity=100):
    """
    Fills the current selection in the active document with the specified hex color.
    fill_opacity should be an integer between 0 and 100.
    """
    try:
        r, g, b = hex_to_rgb(desired_hex)
        fill_color = ps.SolidColor()
        fill_color.rgb.red   = r
        fill_color.rgb.green = g
        fill_color.rgb.blue  = b
        ps.echo(f"Fill color set to RGB({r}, {g}, {b}).")
    except Exception as e:
        ps.echo("Error creating the color: " + str(e))
        return

    try:
        ps.app.activeDocument.selection.fill(
            fill_color,
            ps.ColorBlendMode.NormalBlendColor,
            fill_opacity,
            False
        )
        ps.echo(f"Successfully filled the selection with color {desired_hex} at {fill_opacity}% opacity.")
    except Exception as e:
        ps.echo("Error filling the selection: " + str(e))

def set_text_layer_color(ps, layer_name, desired_hex):
    """
    Sets the text color of the text layer with the given name to the specified hex color.
    """
    try:
        doc = ps.app.activeDocument
        text_layer = doc.ArtLayers[layer_name]
    except Exception as e:
        ps.echo(f"Error: Text layer '{layer_name}' not found: " + str(e))
        return

    try:
        r, g, b = hex_to_rgb(desired_hex)
        text_color = ps.SolidColor()
        text_color.rgb.red = r
        text_color.rgb.green = g
        text_color.rgb.blue = b
        text_layer.TextItem.color = text_color
        ps.echo(f"Set text layer '{layer_name}' color to {desired_hex} (RGB({r}, {g}, {b})).")
    except Exception as e:
        ps.echo("Error setting text layer color: " + str(e))

def clear_bg_color_layer(ps):
    """
    Clears the contents of the Bg_Color layer.
    This simulates selecting the entire layer (CTRL+A) and then clearing (DELETE).
    """
    doc = ps.app.activeDocument
    bg_layer = select_bg_color_layer(ps)
    try:
        doc.selection.selectAll()
        doc.selection.clear()
        ps.echo("Cleared the Bg_Color layer contents.")
    except Exception as e:
        ps.echo("Error clearing Bg_Color layer: " + str(e))

def select_btn_color_layer(ps):
    """
    Selects the layer named "Btn_Color" in the active document.
    If it doesn't exist, creates one and (optionally) positions it above 'Button_Color_Default'.
    Returns the layer.
    """
    doc = ps.app.activeDocument
    try:
        btn_layer = doc.ArtLayers["Btn_Color"]
    except Exception:
        ps.echo("Layer 'Btn_Color' not found. Creating a new one.")
        btn_layer = doc.artLayers.add()
        btn_layer.name = "Btn_Color"
        try:
            default_layer = doc.ArtLayers["Button_Color_Default"]
            btn_layer.move(default_layer, ps.ElementPlacement.PlaceBefore)
        except Exception:
            ps.echo("Default button color layer 'Button_Color_Default' not found. Btn_Color created at top.")
    doc.activeLayer = btn_layer
    ps.echo("The layer 'Btn_Color' is now the active layer.")
    return btn_layer

def clear_btn_color_layer(ps):
    """
    Clears the contents of the Btn_Color layer.
    This simulates selecting the entire layer (CTRL+A) and then clearing (DELETE).
    """
    doc = ps.app.activeDocument
    btn_layer = select_btn_color_layer(ps)
    try:
        doc.selection.selectAll()
        doc.selection.clear()
        ps.echo("Cleared the Btn_Color layer contents.")
    except Exception as e:
        ps.echo("Error clearing Btn_Color layer: " + str(e))

# For isolated testing of color automation, run this module directly.
def main():
    desired_bg_color = "#00FF00"
    desired_text_color = "#000000"

    with Session() as ps:
        if len(ps.app.documents) < 1:
            ps.echo("No active document found. Creating a new document.")
            doc = ps.app.documents.add(
                320, 240, 72, None, ps.NewDocumentMode.NewRGB, ps.DocumentFill.White
            )
        else:
            doc = ps.app.activeDocument

        bg_layer = select_bg_color_layer(ps)
        try:
            doc.selection.selectAll()
            ps.echo("Selected the entire canvas for background fill.")
        except Exception as e:
            ps.echo("Error selecting the canvas: " + str(e))
            return
        fill_active_selection_with_color(ps, desired_bg_color, fill_opacity=100)
        clear_bg_color_layer(ps)

        set_text_layer_color(ps, "Temp_Text_Layer", desired_text_color)
        set_text_layer_color(ps, "Temp_Cta_Layer", desired_text_color)

        # Button automation testing:
        try:
            btn_text_layer = doc.ArtLayers["Temp_Button_Text"]
            btn_text_layer.TextItem.contents = "Test Button"
        except Exception as e:
            ps.echo("Error: 'Temp_Button_Text' layer not found: " + str(e))
        set_text_layer_color(ps, "Temp_Button_Text", "#FF0000")
        btn_layer = select_btn_color_layer(ps)
        try:
            doc.selection.selectAll()
        except Exception as e:
            ps.echo("Error selecting the canvas for button fill: " + str(e))
        else:
            fill_active_selection_with_color(ps, "#00FFFF", fill_opacity=100)
        clear_btn_color_layer(ps)

if __name__ == "__main__":
    main()
