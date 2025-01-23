from util import normalize_doi

lookup_raw = {
    "10.1016/j.biocon.2016.04.014": [
        "researchgate.net/profile/Arthur_Muneza/publication/301936941_Regional_variation_of_the_manifestation_prevalence_and_severity_of_giraffe_skin_disease_A_review_of_an_emerging_disease_in_wild_and_captive_giraffe_populations/links/57449ff608ae9ace8421a52f.pdf"
    ],
    "10.1016/j.tcb.2014.11.005": [
        "doi.org/10.6084/m9.figshare.1409475",
        "figshare.com/articles/An_open_data_ecosystem_for_cell_migration_research_/1409475",
        "doi.org/10.6084/M9.FIGSHARE.3114679",
        "figshare.com/articles/An_open_data_ecosystem_for_cell_migration_research_/3114679",
    ],
    "10.1016/j.vaccine.2014.04.085": [
        "ruvzca.sk/sites/default/files/dodatocne-subory/meta-analysis_vaccin_autism_2014.pdf"
    ],
    "10.1093/nar/gkx1020": [
        "doi.org/10.1093/nar/gkx1020"
    ],
    "10.2307/632037": [
        "pdfs.semanticscholar.org/250a/c6b55d82a496deda1d14af0174dd3ffe4b41.pdf"
    ],
    "10.1080/02640414.2017.1378494": [
      "http://libres.uncg.edu/ir/uncp/f/Effects of mild running on substantia nigra.pdf"
    ],
    "10.1145/3342428.3342662": [
        #  ticket 22288
        "http://hdl.handle.net/11693/52923",
        "https://hdl.handle.net/11511/31020",
        "https://open.metu.edu.tr/bitstream/handle/11511/31020/index.pdf",
    ],
    "10.1016/j.enggeo.2019.105452": [
        "https://eprints.ucm.es/58793/1/Modelling%20earthquake%20rupture%20rates%20in%20fault%20systems%20for%20seismic%20hazard%20assessment.pdf",
    ],
    "10.1038/nphoton.2017.32": [
        "https://iris.unibs.it/bitstream/11379/488968/1/10.1038%40nphoton.2017.32.pdf",
    ],
    "10.1016/j.micromeso.2021.110909": [
        "https://chemrxiv.org/ndownloader/files/22221642",
        "https://s3-eu-west-1.amazonaws.com/itempdf74155353254prod/12073869/General_Cluster_Sorption_Isotherm_v1.pdf",
    ],
    "10.1007/s10530-016-1077-6": [
      "http://dspace.stir.ac.uk/bitstream/1893/23086/1/Roy%20et%20al%202016%20Biol%20Cons%20Author%20Copy.pdf",  # ticket 23034
    ],
    "10.1126/science.abf8003": [
        "https://doi.org/10.1126/science.abf8003"  # tickart 23050, license says it should be OA but it isn't
    ],
    "10.1136/bmj.g3387": [
        "http://www.savetheharlanbeagles.com/ewExternalFiles/bmj-is-animal-research-sufficiently-evidenced-basede-tobea-cornerstone-of-biomedical-research.pdf"
    ],
    "10.1007/978-3-662-05094-1": [
        "https://zenodo.org/record/4454681",
    ],
    "10.1007/978-3-662-44874-8": [
        "https://zenodo.org/record/4454681",
    ],
    '10.4000/books.putc.16867': [
        'https://www.elysee.fr/front/pdf/elysee-module-19159-fr.pdf'
    ],
    '10.1080/14693062.2015.1094729': [
        'http://www.ucsusa.org/sites/default/files/attach/2015/07/The-Climate-Deception-Dossiers.pdf'
    ],
    '10.1386/jwcp.3.1.31_1':
        ['https://www.intellectbooks.com/asset/76511/1/JWCP_CallforTeamMembers_2023_1_.pdf'],

    '10.5153/sro.3693':
        ['http://www.ejustice.just.fgov.be/mopdf/2013/04/11_1.pdf#Page41'],
    '10.29228/jamp.53876':
        ['https://academicmed.org/Uploads/Volume7Issue1/1.%20[4520.%20JAMP_MOGA]%201-5.pdf']
}

location_url_blacklist = {
    'https://pq-static-content.proquest.com/collateral/media2/documents/ebookcentral-dda.pdf'
}


def is_reported_noncompliant_url(dirty_doi, dirty_url):
    if not dirty_url:
        return False

    if dirty_url in location_url_blacklist:
        return True
    my_url = dirty_url.lower()
    for url_fragment in reported_noncompliant_url_fragments(dirty_doi):
        if url_fragment in my_url:
            return True
    return False


def reported_noncompliant_url_fragments(dirty_doi):
    if not dirty_doi:
        return []

    lookup_normalized = {}
    for (doi_key, fragment_list) in lookup_raw.items():
        lookup_normalized[normalize_doi(doi_key)] = [noncompliant_url_fragment.lower() for noncompliant_url_fragment in fragment_list]

    return lookup_normalized.get(normalize_doi(dirty_doi), [])
