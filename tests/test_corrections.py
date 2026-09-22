

import json

# ------------------------------------------------- brand and size (free text)
#
# Every other correctable field is held to a taxonomy enum. These two are not:
# there is no complete list of brands, and no single size system across shirts
# (M), trousers (32) and shoes (UK 9 / EU 42). See migration 0021.


async def _first_garment(api, registered) -> str:
    resp = await api.get("/garments", headers=registered.auth)
    return str(resp.json()[0]["id"])


async def test_brand_and_size_accept_free_text(api, registered) -> None:
    garment_id = await _first_garment(api, registered)
    for field, value in (("brand", "Fabindia"), ("size_label", "UK 9")):
        resp = await api.patch(
            f"/garments/{garment_id}/fields",
            json={"field_name": field, "new_value": value},
            headers=registered.auth,
        )
        assert resp.status_code == 200, resp.text

    detail = await api.get(f"/garments/{garment_id}/detail", headers=registered.auth)
    garment = detail.json()["garment"]
    assert garment["brand"] == "Fabindia"
    assert garment["size_label"] == "UK 9"


async def test_free_text_is_bounded_even_though_it_is_not_validated(api, registered) -> None:
    """There is nothing to validate a brand AGAINST, but there is still a
    column width. A paste of a whole webpage should be a 400 with a reason,
    not a database error surfacing as a 500."""
    resp = await api.patch(
        f"/garments/{await _first_garment(api, registered)}/fields",
        json={"field_name": "brand", "new_value": "x" * 200},
        headers=registered.auth,
    )
    assert resp.status_code == 400
    assert "80 characters" in resp.json()["detail"]


async def test_blank_clears_rather_than_storing_an_empty_string(api, registered) -> None:
    """"I was wrong, I don't know the brand" is a legitimate correction.

    Stored as NULL so the UI has ONE falsy state to render. An empty string
    and a NULL would look identical on screen and different in every query.
    """
    garment_id = await _first_garment(api, registered)
    await api.patch(
        f"/garments/{garment_id}/fields",
        json={"field_name": "brand", "new_value": "Zara"},
        headers=registered.auth,
    )
    await api.patch(
        f"/garments/{garment_id}/fields",
        json={"field_name": "brand", "new_value": "   "},
        headers=registered.auth,
    )
    detail = await api.get(f"/garments/{garment_id}/detail", headers=registered.auth)
    assert detail.json()["garment"]["brand"] is None


async def test_the_vlm_is_never_asked_for_brand_or_size(api) -> None:
    """A vision model reads a logo confidently and wrongly, and a garment
    labelled "Nike" that is not Nike is worse than one with no brand: it is a
    fact the user did not enter and cannot easily disbelieve. Size is usually
    on a care label the photograph does not show at all.

    Asserted against the tag schema so that adding either to the VLM prompt
    fails here, deliberately, rather than quietly shipping invented data.
    """
    from stylist_domain.taxonomy import load_taxonomy
    from stylist_domain.vlm_schema import build_schema, vlm_fields

    taxonomy = load_taxonomy()
    assert "brand" not in vlm_fields(taxonomy)
    assert "size_label" not in vlm_fields(taxonomy)

    # And not in the schema the model is actually sent, which is the thing
    # that would cost money and produce the invented value.
    schema = build_schema(taxonomy, cells=["a"])
    rendered = json.dumps(schema)
    assert '"brand"' not in rendered
    assert '"size_label"' not in rendered
