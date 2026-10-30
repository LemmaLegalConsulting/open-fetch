"""Deterministic screening protocols and stable-ID outcome evaluation.

Protocol text is data, not prompt material: it is never sent through semantic merge.
The English/Spanish translations are conveniences. Clients may render their own text
using ``translation_key`` while returning the invariant protocol/question/choice IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.models.api_models import (
    EffectiveCategory,
    FollowUpAnswer,
    Label,
    MandatoryCategory,
    ScreeningAnswer,
    ScreeningChoice,
    ScreeningQuestion,
    ScreeningResolution,
    ScreeningResult,
)

RiskLevel = str


@dataclass(frozen=True)
class OutcomeDefinition:
    risk_level: RiskLevel
    information_trigger: Optional[str] = None
    mandatory_category_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ChoiceDefinition:
    id: str
    en: str
    es: str
    outcome: OutcomeDefinition


@dataclass(frozen=True)
class ChoiceSuppressionDefinition:
    """Narrative facts that make one displayed choice redundant or impossible."""

    choice_id: str
    patterns: Tuple[str, ...]


@dataclass(frozen=True)
class ProtocolDefinition:
    protocol_id: str
    version: int
    priority: int
    question_id: str
    question_en: str
    question_es: str
    choices: Tuple[ChoiceDefinition, ...]
    candidate_terms: Tuple[str, ...]
    text_patterns: Tuple[str, ...]
    narrative_answer_patterns: Tuple[str, ...] = ()
    choice_suppressions: Tuple[ChoiceSuppressionDefinition, ...] = ()
    enabled: bool = True


CATEGORY_LABELS: Mapping[str, str] = {
    "family.restraining_orders": "Family Law > Restraining Orders",
    "employment.whistleblowers": "Labor & Employment > Whistleblowers - Employee",
    "employment.wrongful_discharge": "Labor & Employment > Wrongful Discharge - Employee",
    "workers_comp.third_party_litigation": "Workers' Comp > Third Party Litigation",
    "wills_trusts.elder_abuse": "Wills & Trusts > Elder Abuse",
    "international.general_immigration_visas": "International Law > General Immigration/Visas",
    "international.deportation": "International Law > Deportation",
    "general_litigation.actions_against_police": "General Litigation > Actions Against Police",
    "general_litigation.tort_claims_act": "General Litigation > Tort Claims Act",
}


def _out(
    risk: RiskLevel,
    trigger: Optional[str] = None,
    *categories: str,
) -> OutcomeDefinition:
    return OutcomeDefinition(risk, trigger, tuple(categories))


PROTOCOLS: Tuple[ProtocolDefinition, ...] = (
    ProtocolDefinition(
        "family_safety.v1",
        1,
        100,
        "family_safety.behavior.v1",
        "Has anyone involved in this family or relationship situation done any of the following?",
        "¿Alguna persona involucrada en esta situación familiar o de pareja ha hecho algo de lo siguiente?",
        (
            ChoiceDefinition(
                "none", "No, none of these", "No, nada de esto", _out("none")
            ),
            ChoiceDefinition(
                "controlling_or_threatening",
                "Controlled, monitored, stalked, threatened, or repeatedly harassed me or someone else",
                "Me controló, vigiló, acosó, amenazó o molestó repetidamente a mí o a otra persona",
                _out(
                    "informational",
                    "domestic_violence_safety_resources",
                    "family.restraining_orders",
                ),
            ),
            ChoiceDefinition(
                "physical_or_sexual_harm",
                "Physically hurt, sexually harmed, strangled, or threatened someone with a weapon",
                "Lastimó físicamente, agredió sexualmente, estranguló o amenazó a alguien con un arma",
                _out(
                    "elevated",
                    "domestic_violence_elevated_risk",
                    "family.restraining_orders",
                ),
            ),
            ChoiceDefinition(
                "immediate_danger",
                "Someone may be in immediate danger now",
                "Alguien puede estar en peligro inmediato ahora",
                _out(
                    "urgent",
                    "domestic_violence_immediate_danger",
                    "family.restraining_orders",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure",
                "No estoy seguro/a",
                _out("informational", "domestic_violence_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        (
            "divorce",
            "separation",
            "child custody",
            "visitation",
            "parenting time",
            "paternity",
            "restraining order",
        ),
        (
            r"\b(spouse|husband|wife|boyfriend|girlfriend|cónyuge|espos[oa]|expareja|novi[oa])\b|\b(my|current|former|ex|romantic|intimate|mi|actual|anterior) (partner|pareja)\b",
            r"\b(custody|parenting time|paternity|divorc\w*|separat\w*|household conflict|protective order|domestic violence|abuse|custodia|tiempo de crianza|paternidad|conflicto familiar|orden de protección|violencia doméstica|maltrato)\b",
        ),
        (
            r"\b(stalk(?:ed|ing)|threaten(?:ed|ing)|harass(?:ed|ing)|strangl(?:ed|ing)|chok(?:ed|ing)|hit me|beat me|physically (?:hurt|assaulted)|sexually (?:harmed|assaulted)|rape(?:d)?|threatened (?:me|us|someone) with (?:a )?weapon|immediate danger|afraid for (?:my|our|their) safety)\b",
            r"\b(acech(?:ó|aba)|amenaz(?:ó|aba)|acos(?:ó|aba)|estrangul(?:ó|aba)|golpeó|me lastimó físicamente|agredi(?:ó|da) sexualmente|viol(?:ó|ada)|amenazó con un arma|peligro inmediato|temo por (?:mi|nuestra|su) seguridad)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "controlling_or_threatening",
                (
                    r"\b(?:no|never any)\s+(control(?:ling)?|monitor(?:ing)?|stalk(?:ing)?|threats?|harassment)\b",
                    r"\b(?:no hay|nunca hubo|sin)\s+(control|vigilancia|acecho|amenazas?|acoso)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "physical_or_sexual_harm",
                (
                    r"\b(?:no|never any)\s+(physical|sexual)\b.{0,20}\b(harm|violence|assault|abuse)\b|\bno weapons?\b",
                    r"\b(?:no hay|nunca hubo|sin)\s+(daño|violencia|agresión|abuso)\b.{0,20}\b(físic[oa]|sexual)\b|\bsin armas?\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "immediate_danger",
                (
                    r"\b(no immediate danger|not in immediate danger|safe (?:right )?now|currently safe)\b",
                    r"\b(no hay peligro inmediato|no está en peligro inmediato|a salvo ahora|actualmente a salvo)\b",
                ),
            ),
        ),
    ),
    ProtocolDefinition(
        "elder_exploitation.v1",
        1,
        90,
        "elder_exploitation.behavior.v1",
        "Has anyone involved in the older or vulnerable adult’s care or finances done any of the following?",
        "¿Alguna persona involucrada en el cuidado o las finanzas del adulto mayor o vulnerable ha hecho algo de lo siguiente?",
        (
            ChoiceDefinition(
                "none", "No, none of these", "No, nada de esto", _out("none")
            ),
            ChoiceDefinition(
                "pressure_or_control",
                "Pressured or controlled decisions about money, property, accounts, a will, trust, or power of attorney",
                "Presionó o controló decisiones sobre dinero, bienes, cuentas, un testamento, fideicomiso o poder notarial",
                _out(
                    "informational",
                    "elder_exploitation_resources",
                    "wills_trusts.elder_abuse",
                ),
            ),
            ChoiceDefinition(
                "taking_assets",
                "Took, transferred, hid, or used money or property without permission",
                "Tomó, transfirió, ocultó o usó dinero o bienes sin permiso",
                _out(
                    "elevated",
                    "elder_exploitation_elevated",
                    "wills_trusts.elder_abuse",
                ),
            ),
            ChoiceDefinition(
                "basic_needs_or_safety",
                "Left the person without access to money, housing, medication, care, or other basic needs",
                "Dejó a la persona sin acceso a dinero, vivienda, medicamentos, cuidados u otras necesidades básicas",
                _out(
                    "urgent",
                    "vulnerable_adult_immediate_assistance",
                    "wills_trusts.elder_abuse",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure",
                "No estoy seguro/a",
                _out("informational", "elder_exploitation_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        ("wills & trusts", "elder abuse", "guardianship", "conservatorship", "probate"),
        (
            r"\b(elder|elderly|older adult|vulnerable adult|adulto mayor|persona mayor|adulto vulnerable)\b",
            r"\b(guardianship|conservatorship|power of attorney|representative payee|last will|will and testament|inheritance|caregiver|tutela|curatela|poder notarial|beneficiario representativo|testamento|fideicomiso|herencia|cuidador)\b|\b(a|the|family) trust\b|\btrust account\b",
        ),
        (
            r"\b(pressur(?:ed|ing)|coerc(?:ed|ing)|control(?:led|ling))\b.{0,100}\b(money|property|account|will|trust|power of attorney)\b",
            r"\b(stole|taken|took|transferred|hid|used)\b.{0,80}\b(money|funds|assets|property)\b",
            r"\b(without|denied)\b.{0,80}\b(money|housing|medication|medicine|care|basic needs)\b",
            r"\b(presion(?:ó|aba)|coaccion(?:ó|aba)|control(?:ó|aba))\b.{0,100}\b(dinero|bienes|cuenta|testamento|fideicomiso|poder notarial)\b",
            r"\b(robó|tomó|transfirió|ocultó|usó)\b.{0,80}\b(dinero|fondos|activos|bienes|propiedad)\b",
            r"\b(sin acceso|negó)\b.{0,80}\b(dinero|vivienda|medicamentos|cuidados|necesidades básicas)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "pressure_or_control",
                (
                    r"\b(?:no|never any)\s+(pressure|coercion|control)\b.{0,60}\b(finances|money|property|will|trust|power of attorney)\b",
                    r"\b(?:no hay|nunca hubo|sin)\s+(presión|coacción|control)\b.{0,60}\b(finanzas|dinero|bienes|testamento|fideicomiso|poder notarial)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "taking_assets",
                (
                    r"\b(?:nothing (?:was |has been )?|no assets (?:were |have been )?)(stolen|taken|transferred|hidden|missing|used without permission)\b",
                    r"\b(?:nada (?:fue |ha sido )?)(robado|tomado|transferido|oculto|desaparecido|usado sin permiso)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "basic_needs_or_safety",
                (
                    r"\b(has|have|still has|still have) access to\b.{0,60}\b(money|housing|medication|care|basic needs)\b",
                    r"\b(tiene|tienen|aún tiene|todavía tiene) acceso a\b.{0,60}\b(dinero|vivienda|medicamentos|cuidados|necesidades básicas)\b",
                ),
            ),
        ),
    ),
    ProtocolDefinition(
        "immigration_consequences.v1",
        1,
        80,
        "immigration_consequences.effect.v1",
        "Could this situation affect anyone’s ability to stay in the United States, renew immigration status, obtain a visa or green card, become a citizen, or avoid deportation?",
        "¿Podría esta situación afectar la capacidad de alguien para permanecer en Estados Unidos, renovar su estatus migratorio, obtener una visa o tarjeta de residencia, hacerse ciudadano/a o evitar la deportación?",
        (
            ChoiceDefinition("no", "No", "No", _out("none")),
            ChoiceDefinition(
                "application_or_status",
                "Yes, it may affect an application, visa, green card, citizenship, or current status",
                "Sí, puede afectar una solicitud, visa, tarjeta de residencia, ciudadanía o estatus actual",
                _out(
                    "informational",
                    "immigration_status_consequences",
                    "international.general_immigration_visas",
                ),
            ),
            ChoiceDefinition(
                "removal_or_deportation",
                "Yes, someone has received an ICE, immigration-court, removal, or deportation notice",
                "Sí, alguien recibió un aviso de ICE, del tribunal de inmigración, de expulsión o de deportación",
                _out(
                    "elevated",
                    "immigration_removal_time_sensitive",
                    "international.deportation",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure",
                "No estoy seguro/a",
                _out("informational", "immigration_consequences_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        (
            "international law > deportation",
            "international law > general immigration",
            "immigration",
            "visa",
        ),
        (
            r"\b(immigration status|citizenship|visa|green card|sponsor(?:ship)?|ice|immigration court|deportation|removal proceedings?|estatus migratorio|ciudadanía|tarjeta de residencia|patrocinio|tribunal de inmigración|deportación|proceso de expulsión)\b",
        ),
        (
            r"\b(received|got|served with)\b.{0,60}\b(ice|immigration court|removal|deportation)\b.{0,30}\b(notice|papers|summons)\b",
            r"\b(facing|in|scheduled for)\b.{0,40}\b(deportation|removal proceedings?|immigration court)\b",
            r"\b(affect|jeopardize|lose|renew|denied|deny)\b.{0,60}\b(immigration status|visa|green card|citizenship)\b",
            r"\b(recibí|recibió|entregaron)\b.{0,60}\b(ice|tribunal de inmigración|expulsión|deportación)\b.{0,30}\b(aviso|documentos|citación)\b",
            r"\b(enfrenta|en proceso de|audiencia de)\b.{0,40}\b(deportación|expulsión|tribunal de inmigración)\b",
            r"\b(afectar|perder|renovar|negaron|denegar)\b.{0,60}\b(estatus migratorio|visa|tarjeta de residencia|ciudadanía)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "application_or_status",
                (
                    r"\b(will not|won't|cannot) affect\b.{0,60}\b(immigration status|visa|green card|citizenship)\b",
                    r"\b(no afectará|no puede afectar)\b.{0,60}\b(estatus migratorio|visa|tarjeta de residencia|ciudadanía)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "removal_or_deportation",
                (
                    r"\b(no|never received|have not received|has not received)\s+(?:an?\s+)?(ice|immigration court|removal|deportation)\b.{0,20}\b(notice|papers|case|proceedings)\b",
                    r"\b(no|nunca recibió|no ha recibido)\s+(ice|tribunal de inmigración|expulsión|deportación)\b.{0,20}\b(aviso|documentos|caso|proceso)\b",
                ),
            ),
        ),
    ),
    ProtocolDefinition(
        "police_government_claim.v1",
        1,
        70,
        "police_government_claim.help.v1",
        "Which best describes the help you may need after the police or government encounter?",
        "¿Qué opción describe mejor la ayuda que podría necesitar después del encuentro con la policía o el gobierno?",
        (
            ChoiceDefinition(
                "criminal_defense_only",
                "Help defending against a charge, citation, probation issue, or criminal case",
                "Ayuda para defenderme de un cargo, citación, problema de libertad condicional o caso penal",
                _out("none"),
            ),
            ChoiceDefinition(
                "police_misconduct",
                "Help concerning excessive force, false arrest, an unlawful search, malicious prosecution, or other police misconduct",
                "Ayuda relacionada con fuerza excesiva, arresto ilegal, registro ilegal, proceso malicioso u otra conducta policial indebida",
                _out(
                    "elevated",
                    "police_misconduct_time_sensitive",
                    "general_litigation.actions_against_police",
                ),
            ),
            ChoiceDefinition(
                "government_injury_or_loss",
                "Help for an injury or loss caused by a government agency or government employee",
                "Ayuda por una lesión o pérdida causada por una agencia o empleado del gobierno",
                _out(
                    "elevated",
                    "government_claim_time_sensitive",
                    "general_litigation.tort_claims_act",
                ),
            ),
            ChoiceDefinition(
                "both_criminal_and_misconduct",
                "Both defending a criminal case and addressing possible police misconduct",
                "Tanto defender un caso penal como abordar una posible conducta policial indebida",
                _out(
                    "elevated",
                    "police_misconduct_and_criminal_case",
                    "general_litigation.actions_against_police",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure",
                "No estoy seguro/a",
                _out("informational", "police_encounter_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        ("actions against police", "tort claims act"),
        (
            r"\b(police|officer|arrest|detention|detained|search|excessive force|false arrest|government agency|government employee|state agency|policía|agente|arresto|detención|detenido|registro|fuerza excesiva|agencia gubernamental|empleado del gobierno)\b",
        ),
        (
            r"\b(police|officer)\b.{0,80}\b(excessive force|false arrest|unlawful search|illegal search|malicious prosecution|misconduct|beat|injured|hurt)\b",
            r"\b(government agency|government employee|city employee|county employee|state employee)\b.{0,80}\b(caused|injured|damaged|destroyed|lost)\b",
            r"\b(defend(?:ing)?|defense)\b.{0,50}\b(charge|citation|probation|criminal case)\b",
            r"\b(policía|agente)\b.{0,80}\b(fuerza excesiva|arresto ilegal|registro ilegal|proceso malicioso|conducta indebida|golpeó|lesionó|lastimó)\b",
            r"\b(agencia gubernamental|empleado del gobierno|empleado municipal|empleado estatal)\b.{0,80}\b(causó|lesionó|dañó|destruyó|perdió)\b",
            r"\b(defender|defensa)\b.{0,50}\b(cargo|citación|libertad condicional|caso penal)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "criminal_defense_only",
                (
                    r"\b(no|not facing|without)\s+(?:any\s+)?(?:criminal\s+)?(charges?|citation|probation|criminal case)\b",
                    r"\b(no|sin)\s+(cargos?|citación|libertad condicional|caso penal)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "both_criminal_and_misconduct",
                (
                    r"\b(no|not facing|without)\s+(?:any\s+)?(?:criminal\s+)?(charges?|citation|probation|criminal case)\b|\b(no|not alleging)\b.{0,30}\b(police misconduct|excessive force|false arrest|unlawful search)\b",
                    r"\b(no|sin)\s+(cargos?|citación|libertad condicional|caso penal)\b|\b(no)\b.{0,30}\b(conducta policial indebida|fuerza excesiva|arresto ilegal|registro ilegal)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "police_misconduct",
                (
                    r"\b(no|not alleging)\b.{0,30}\b(police misconduct|excessive force|false arrest|unlawful search)\b",
                    r"\b(no)\b.{0,30}\b(conducta policial indebida|fuerza excesiva|arresto ilegal|registro ilegal)\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "government_injury_or_loss",
                (
                    r"\b(no|without)\b.{0,30}\b(injury|loss|damage)\b.{0,50}\b(government|agency|employee)\b",
                    r"\b(no|sin)\s+(lesión|pérdida|daño)\b.{0,50}\b(gobierno|agencia|empleado)\b",
                ),
            ),
        ),
    ),
    ProtocolDefinition(
        "employment_retaliation.v1",
        1,
        60,
        "employment_retaliation.report.v1",
        "Did your employer fire, discipline, threaten, reduce your hours, or otherwise act against you after you reported illegal, unsafe, fraudulent, or dishonest conduct?",
        "¿Su empleador lo/la despidió, disciplinó, amenazó, redujo sus horas o tomó otras represalias después de que usted denunció una conducta ilegal, insegura, fraudulenta o deshonesta?",
        (
            ChoiceDefinition("no", "No", "No", _out("none")),
            ChoiceDefinition(
                "yes_not_fired",
                "Yes, but I was not fired or forced to quit",
                "Sí, pero no fui despedido/a ni obligado/a a renunciar",
                _out(
                    "elevated",
                    "employment_retaliation_time_sensitive",
                    "employment.whistleblowers",
                ),
            ),
            ChoiceDefinition(
                "yes_fired",
                "Yes, and I was fired or forced to quit",
                "Sí, y fui despedido/a u obligado/a a renunciar",
                _out(
                    "elevated",
                    "employment_retaliation_time_sensitive",
                    "employment.whistleblowers",
                    "employment.wrongful_discharge",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure whether the employer’s action was connected to my report",
                "No estoy seguro/a de que la acción del empleador estuviera relacionada con mi denuncia",
                _out("informational", "employment_retaliation_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        ("labor & employment", "whistleblower", "wrongful discharge"),
        (
            r"\b(employer|employee|workplace|job|fired|terminated|demoted|reduced hours|forced to quit|forced resignation|empleador|empleado|lugar de trabajo|trabajo|despedido|degradado|redujo mis horas|obligado a renunciar)\b",
            r"\b(report(?:ed)?|unsafe|fraud|illegal conduct|dishonest|retaliat|denunci|insegur|fraude|conducta ilegal|deshonest|represalia)\b",
        ),
        (
            r"\b(fired|terminated|disciplined|demoted|threatened|reduced (?:my )?hours|forced (?:me )?to quit|retaliated)\b.{0,120}\b(after|because|for)\b.{0,80}\b(report(?:ed|ing)?|whistleblowing|complain(?:ed|ing)?)\b",
            r"\b(report(?:ed|ing)?|whistleblowing|complain(?:ed|ing)?)\b.{0,120}\b(then|after|because|so|and)\b.{0,80}\b(fired|terminated|disciplined|demoted|threatened|reduced (?:my )?hours|forced (?:me )?to quit|retaliated)\b",
            r"\b(despid(?:ió|ieron|o)|disciplin(?:ó|aron)|degrad(?:ó|aron)|amenaz(?:ó|aron)|redujo mis horas|obligó a renunciar|tomó represalias)\b.{0,120}\b(después|porque|por)\b.{0,80}\b(denunci(?:é|ó|ar)|inform(?:é|ó)|quej(?:é|ó))\b",
            r"\b(denunci(?:é|ó|ar)|inform(?:é|ó)|quej(?:é|ó))\b.{0,120}\b(después|entonces|porque|y)\b.{0,80}\b(despid(?:ió|ieron|o)|disciplin(?:ó|aron)|degrad(?:ó|aron)|amenaz(?:ó|aron)|redujo mis horas|obligó a renunciar|represalias)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "yes_not_fired",
                (
                    r"\b(was|got|have been) (fired|terminated)|\b(fired|terminated) me\b|\bforced (?:me )?to quit\b",
                    r"\b(fui|me) despedid[oa]|\bme obligó a renunciar\b",
                ),
            ),
            ChoiceSuppressionDefinition(
                "yes_fired",
                (
                    r"\b(not|was not|wasn't|never) (fired|terminated)|\bnot forced to quit\b",
                    r"\b(no fui|nunca fui) despedid[oa]|\bno me obligó a renunciar\b",
                ),
            ),
        ),
    ),
    ProtocolDefinition(
        "work_injury_third_party.v1",
        1,
        50,
        "work_injury_third_party.cause.v1",
        "Was your work injury caused by someone other than your employer or a coworker—for example, a driver, contractor, property owner, or product manufacturer?",
        "¿Su lesión laboral fue causada por alguien que no fuera su empleador o un compañero de trabajo, por ejemplo, un conductor, contratista, propietario o fabricante de un producto?",
        (
            ChoiceDefinition("no", "No", "No", _out("none")),
            ChoiceDefinition(
                "yes",
                "Yes",
                "Sí",
                _out(
                    "informational",
                    "work_injury_possible_third_party_claim",
                    "workers_comp.third_party_litigation",
                ),
            ),
            ChoiceDefinition(
                "not_sure",
                "I am not sure",
                "No estoy seguro/a",
                _out("informational", "work_injury_third_party_general_information"),
            ),
            ChoiceDefinition(
                "prefer_not",
                "I prefer not to answer",
                "Prefiero no responder",
                _out("none"),
            ),
        ),
        ("workers' comp", "workers comp", "work injury", "occupational accident"),
        (
            r"\b(work(?:place)? injury|injured at work|workers['’]? comp(?:ensation)?|occupational accident|driving for work|on the job injury|lesión laboral|lesionado en el trabajo|compensación laboral|accidente laboral|conducía por trabajo)\b",
        ),
        (
            r"\b(driver|contractor|property owner|manufacturer|customer|vendor)\b.{0,80}\b(hit|struck|injured|caused|made|defective|faulty)\b",
            r"\b(hit|struck|injured|caused)\b.{0,80}\bby (?:a |the )?(driver|contractor|property owner|manufacturer|customer|vendor)\b",
            r"\b(conductor|contratista|propietario|fabricante|cliente|proveedor)\b.{0,80}\b(golpeó|lesionó|causó|defectuoso)\b",
            r"\b(golpeado|lesionado|causado)\b.{0,80}\bpor (?:un |una |el |la )?(conductor|contratista|propietario|fabricante|cliente|proveedor)\b",
        ),
        (
            ChoiceSuppressionDefinition(
                "yes",
                (
                    r"\b(caused by|fault of)\s+(my employer|the employer|a coworker|my coworker)\b",
                    r"\b(causad[oa] por|culpa de)\s+(mi empleador|el empleador|un compañero|mi compañero)\b",
                ),
            ),
        ),
    ),
)


PROTOCOL_BY_ID: Mapping[str, ProtocolDefinition] = {
    protocol.protocol_id: protocol for protocol in PROTOCOLS
}


# Explicit narrative disclosures are choice-specific so the API can explain why a
# question disappeared and can still return the corresponding warning/routing data.
NARRATIVE_DISCLOSURES: Mapping[str, Tuple[ChoiceSuppressionDefinition, ...]] = {
    "family_safety.v1": (
        ChoiceSuppressionDefinition(
            "immediate_danger",
            (
                r"\b(immediate danger|danger right now|unsafe right now)\b",
                r"\b(peligro inmediato|peligro ahora|en riesgo ahora)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "physical_or_sexual_harm",
            (
                r"\b(strangl(?:ed|ing)|chok(?:ed|ing)|hit me|beat me|physically (?:hurt|assaulted)|sexually (?:harmed|assaulted)|rape(?:d)?|threatened (?:me|us|someone) with (?:a )?weapon)\b",
                r"\b(estrangul(?:ó|aba)|golpeó|me lastimó físicamente|agredi(?:ó|da) sexualmente|viol(?:ó|ada)|amenazó con un arma)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "controlling_or_threatening",
            (
                r"\b(stalk(?:ed|ing)|threaten(?:ed|ing)|harass(?:ed|ing)|controlled me|controls me|controlling me|trying to control me|monitored me|monitors me|monitoring me)\b",
                r"\b(acech(?:ó|aba)|amenaz(?:ó|aba)|acos(?:ó|aba)|me controló|me controla|trat(?:ó|a) de controlarme|me vigiló|me vigila)\b",
            ),
        ),
    ),
    "elder_exploitation.v1": (
        ChoiceSuppressionDefinition(
            "basic_needs_or_safety",
            (
                r"\b(without|denied)\b.{0,80}\b(money|housing|medication|medicine|care|basic needs)\b",
                r"\b(sin acceso|negó)\b.{0,80}\b(dinero|vivienda|medicamentos|cuidados|necesidades básicas)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "taking_assets",
            (
                r"\b(stole|transferred|hid)\b.{0,80}\b(money|funds|assets|property)\b|\b(used|took)\b.{0,80}\b(money|funds|assets|property)\b.{0,40}\b(without permission|without consent)\b",
                r"\b(robó|transfirió|ocultó)\b.{0,80}\b(dinero|fondos|activos|bienes|propiedad)\b|\b(usó|tomó)\b.{0,80}\b(dinero|fondos|activos|bienes|propiedad)\b.{0,40}\b(sin permiso|sin consentimiento)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "pressure_or_control",
            (
                r"\b(pressur(?:ed|ing)|coerc(?:ed|ing)|control(?:led|ling))\b.{0,100}\b(money|property|account|will|trust|power of attorney)\b",
                r"\b(presion(?:ó|aba)|coaccion(?:ó|aba)|control(?:ó|aba))\b.{0,100}\b(dinero|bienes|cuenta|testamento|fideicomiso|poder notarial)\b",
            ),
        ),
    ),
    "immigration_consequences.v1": (
        ChoiceSuppressionDefinition(
            "removal_or_deportation",
            (
                r"\b(received|got|served with)\b.{0,60}\b(ice|immigration court|removal|deportation)\b.{0,30}\b(notice|papers|summons)\b|\b(facing|in|scheduled for)\b.{0,40}\b(deportation|removal proceedings?|immigration court)\b",
                r"\b(recibí|recibió|entregaron)\b.{0,60}\b(ice|tribunal de inmigración|expulsión|deportación)\b.{0,30}\b(aviso|documentos|citación)\b|\b(enfrenta|en proceso de|audiencia de)\b.{0,40}\b(deportación|expulsión|tribunal de inmigración)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "application_or_status",
            (
                r"\b(affect|jeopardize|lose|renew|denied|deny)\b.{0,60}\b(immigration status|visa|green card|citizenship)\b",
                r"\b(afectar|perder|renovar|negaron|denegar)\b.{0,60}\b(estatus migratorio|visa|tarjeta de residencia|ciudadanía)\b",
            ),
        ),
    ),
    "police_government_claim.v1": (
        ChoiceSuppressionDefinition(
            "both_criminal_and_misconduct",
            (
                r"\b(defend(?:ing)?|defense)\b.{0,60}\b(charge|criminal case)\b.{0,120}\b(police misconduct|excessive force|false arrest|unlawful search)\b",
                r"\b(defender|defensa)\b.{0,60}\b(cargo|caso penal)\b.{0,120}\b(conducta policial indebida|fuerza excesiva|arresto ilegal|registro ilegal)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "police_misconduct",
            (
                r"\b(police|officer)\b.{0,80}\b(used excessive force|falsely arrested|conducted an unlawful search|illegally searched|maliciously prosecuted|committed misconduct|beat me|injured me|hurt me)\b",
                r"\b(policía|agente)\b.{0,80}\b(usó fuerza excesiva|arrestó ilegalmente|realizó un registro ilegal|registró ilegalmente|inició un proceso malicioso|cometió conducta indebida|me golpeó|me lesionó|me lastimó)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "government_injury_or_loss",
            (
                r"\b(government agency|government employee|city employee|county employee|state employee)\b.{0,80}\b(caused (?:an? )?(?:injury|loss|damage)|injured me|damaged my|destroyed my)\b",
                r"\b(agencia gubernamental|empleado del gobierno|empleado municipal|empleado estatal)\b.{0,80}\b(causó (?:una? )?(?:lesión|pérdida|daño)|me lesionó|dañó mi|destruyó mi)\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "criminal_defense_only",
            (
                r"\b(defend(?:ing)?|defense)\b.{0,50}\b(charge|citation|probation|criminal case)\b",
                r"\b(defender|defensa)\b.{0,50}\b(cargo|citación|libertad condicional|caso penal)\b",
            ),
        ),
    ),
    "employment_retaliation.v1": (
        ChoiceSuppressionDefinition(
            "yes_fired",
            (
                r"\b(fired|terminated|forced (?:me )?to quit)\b.{0,120}\b(after|because|for)\b.{0,80}\b(report(?:ed|ing)?|whistleblowing|complain(?:ed|ing)?)\b|\b(report(?:ed|ing)?|whistleblowing|complain(?:ed|ing)?)\b.{0,120}\b(then|after|because|so|and)\b.{0,80}\b(fired|terminated|forced (?:me )?to quit)\b",
                r"\b(despid(?:ió|ieron|o)|obligó a renunciar)\b.{0,120}\b(después|porque|por)\b.{0,80}\b(denunci(?:é|ó|ar)|inform(?:é|ó)|quej(?:é|ó))\b",
            ),
        ),
        ChoiceSuppressionDefinition(
            "yes_not_fired",
            (
                r"\b(disciplined|demoted|threatened|reduced (?:my )?hours|retaliated)\b.{0,120}\b(after|because|for)\b.{0,80}\b(report(?:ed|ing)?|whistleblowing|complain(?:ed|ing)?)\b",
                r"\b(disciplin(?:ó|aron)|degrad(?:ó|aron)|amenaz(?:ó|aron)|redujo mis horas|tomó represalias)\b.{0,120}\b(después|porque|por)\b.{0,80}\b(denunci(?:é|ó|ar)|inform(?:é|ó)|quej(?:é|ó))\b",
            ),
        ),
    ),
    "work_injury_third_party.v1": (
        ChoiceSuppressionDefinition(
            "yes",
            (
                r"\b(driver|contractor|property owner|manufacturer|customer|vendor)\b.{0,80}\b(hit|struck|injured|caused|made|defective|faulty)\b|\b(hit|struck|injured|caused)\b.{0,80}\bby (?:a |the )?(driver|contractor|property owner|manufacturer|customer|vendor)\b",
                r"\b(conductor|contratista|propietario|fabricante|cliente|proveedor)\b.{0,80}\b(golpeó|lesionó|causó|defectuoso)\b",
            ),
        ),
    ),
}


RISK_PRIORITY = {"none": 0, "informational": 1, "elevated": 2, "urgent": 3}

EXCLUSIVE_CHOICE_IDS = {
    "none",
    "no",
    "not_sure",
    "prefer_not",
    "criminal_defense_only",
}
MUTUALLY_EXCLUSIVE_CHOICE_PAIRS = {
    frozenset({"yes_not_fired", "yes_fired"}),
}
NEGATIVE_CHOICE_IDS_BY_PROTOCOL = {
    "family_safety.v1": "none",
    "elder_exploitation.v1": "none",
    "immigration_consequences.v1": "no",
    "employment_retaliation.v1": "no",
    "work_injury_third_party.v1": "no",
}
NON_SUBSTANTIVE_CHOICE_IDS = {"none", "no", "not_sure", "prefer_not"}


def _choice_map(protocol: ProtocolDefinition) -> Dict[str, ChoiceDefinition]:
    return {choice.id: choice for choice in protocol.choices}


def validate_answers(answers: Sequence[ScreeningAnswer]) -> None:
    """Reject unknown or mismatched IDs instead of interpreting display text."""
    seen = set()
    for answer in answers:
        protocol = PROTOCOL_BY_ID.get(answer.protocol_id)
        if protocol is None:
            raise ValueError(f"Unknown screening protocol_id: {answer.protocol_id}")
        if answer.question_id != protocol.question_id:
            raise ValueError(
                f"question_id {answer.question_id!r} does not belong to {answer.protocol_id!r}"
            )
        selected_choice_ids = answer.selected_choice_ids()
        for choice_id in selected_choice_ids:
            if choice_id not in _choice_map(protocol):
                raise ValueError(
                    f"Unknown choice_id {choice_id!r} for {answer.protocol_id!r}"
                )
        selected = set(selected_choice_ids)
        if len(selected) > 1 and selected & EXCLUSIVE_CHOICE_IDS:
            raise ValueError(
                f"Exclusive choice cannot be combined with other choices for {answer.protocol_id!r}"
            )
        if any(pair <= selected for pair in MUTUALLY_EXCLUSIVE_CHOICE_PAIRS):
            raise ValueError(
                f"Mutually exclusive choices selected for {answer.protocol_id!r}"
            )
        key = (answer.protocol_id, answer.question_id)
        if key in seen:
            raise ValueError(f"Duplicate screening answer for {answer.question_id!r}")
        seen.add(key)


def evaluate_answers(answers: Sequence[ScreeningAnswer]) -> List[ScreeningResult]:
    validate_answers(answers)
    results: List[ScreeningResult] = []
    for answer in answers:
        protocol = PROTOCOL_BY_ID[answer.protocol_id]
        for choice_id in answer.selected_choice_ids():
            outcome = _choice_map(protocol)[choice_id].outcome
            results.append(
                ScreeningResult(
                    protocol_id=protocol.protocol_id,
                    protocol_version=protocol.version,
                    question_id=protocol.question_id,
                    choice_id=choice_id,
                    risk_level=outcome.risk_level,
                    information_trigger=outcome.information_trigger,
                    mandatory_category_ids=list(outcome.mandatory_category_ids),
                )
            )
    return results


def _eligible(protocol: ProtocolDefinition, text: str, labels: Iterable[str]) -> bool:
    lowered_labels = [label.casefold() for label in labels]
    if any(
        term in label for term in protocol.candidate_terms for label in lowered_labels
    ):
        return True
    lowered_text = text.casefold()
    return any(re.search(pattern, lowered_text) for pattern in protocol.text_patterns)


def eligible_protocols(
    problem_description: str, candidate_labels: Iterable[str]
) -> List[ProtocolDefinition]:
    """Return enabled protocols activated by narrative text or candidate labels."""
    labels = list(candidate_labels)
    return [
        protocol
        for protocol in PROTOCOLS
        if protocol.enabled and _eligible(protocol, problem_description, labels)
    ]


def llm_resolution_contracts(
    problem_description: str, candidate_labels: Iterable[str]
) -> List[Dict[str, Any]]:
    """Build the stable-ID-only protocol contract supplied to the LLM checker."""
    return [
        {
            "protocol_id": protocol.protocol_id,
            "protocol_version": protocol.version,
            "question_id": protocol.question_id,
            "question": protocol.question_en,
            "choices": [
                {"choice_id": choice.id, "label": choice.en}
                for choice in protocol.choices
            ],
        }
        for protocol in eligible_protocols(problem_description, candidate_labels)
    ]


def _matches_unnegated(text: str, patterns: Iterable[str]) -> bool:
    """Match an explicit disclosure while rejecting nearby negation."""
    lowered_text = text.casefold()
    for pattern in patterns:
        for match in re.finditer(pattern, lowered_text):
            nearby_prefix = lowered_text[max(0, match.start() - 30) : match.start()]
            matched_text = match.group(0)
            if re.search(
                r"\b(no|not|never|nunca)\b(?:\W+\w+){0,3}\W*$",
                nearby_prefix,
            ):
                continue
            if re.search(r"\b(no|not|never|nunca)\b", matched_text):
                continue
            return True
    return False


def _disclosed_choice_ids(protocol: ProtocolDefinition, text: str) -> set[str]:
    return {
        disclosure.choice_id
        for disclosure in NARRATIVE_DISCLOSURES.get(protocol.protocol_id, ())
        if _matches_unnegated(text, disclosure.patterns)
    }


def _narrative_already_answers(protocol: ProtocolDefinition, text: str) -> bool:
    """Return true only for an explicit choice-level narrative disclosure."""
    return bool(_disclosed_choice_ids(protocol, text))


def _suppression_matches(pattern: str, text: str) -> bool:
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 60) : match.start()]
        if re.search(
            r"\b(?:not|no|never|cannot|nunca|sin)\b(?:\W+\w+){0,3}\W*$|n['’]t\s+(?:\w+\s+){0,3}$",
            prefix,
        ):
            continue
        return True
    return False


def _suppressed_choice_ids(protocol: ProtocolDefinition, text: str) -> set[str]:
    """Find choices made redundant or impossible by explicit narrative facts."""
    lowered_text = text.casefold()
    return {
        suppression.choice_id
        for suppression in protocol.choice_suppressions
        # These choices combine several risks. Denying one does not rule out all.
        if not (
            protocol.protocol_id == "family_safety.v1"
            and suppression.choice_id
            in {"controlling_or_threatening", "physical_or_sexual_harm"}
        )
        if any(
            _suppression_matches(pattern, lowered_text)
            for pattern in suppression.patterns
        )
    }


def resolve_narrative(
    problem_description: str,
    candidate_labels: Iterable[str],
) -> List[ScreeningResolution]:
    """Return auditable choice-level statuses inferred by deterministic text rules."""
    labels = list(candidate_labels)
    resolutions: List[ScreeningResolution] = []
    for protocol in PROTOCOLS:
        if not protocol.enabled or not _eligible(protocol, problem_description, labels):
            continue
        disclosed = _disclosed_choice_ids(protocol, problem_description)
        if "yes_fired" in disclosed:
            disclosed.discard("yes_not_fired")
        ruled_out = _suppressed_choice_ids(protocol, problem_description) - disclosed
        if disclosed:
            negative_choice_id = NEGATIVE_CHOICE_IDS_BY_PROTOCOL.get(
                protocol.protocol_id
            )
            if negative_choice_id:
                ruled_out.add(negative_choice_id)
        choice_map = _choice_map(protocol)
        ordered_choice_ids = [choice.id for choice in protocol.choices]
        for choice_id in ordered_choice_ids:
            if choice_id not in disclosed:
                continue
            outcome = choice_map[choice_id].outcome
            resolutions.append(
                ScreeningResolution(
                    protocol_id=protocol.protocol_id,
                    protocol_version=protocol.version,
                    question_id=protocol.question_id,
                    choice_id=choice_id,
                    status="disclosed",
                    source="narrative_keyword",
                    risk_level=outcome.risk_level,
                    information_trigger=outcome.information_trigger,
                    mandatory_category_ids=list(outcome.mandatory_category_ids),
                )
            )
        for choice_id in ordered_choice_ids:
            if choice_id not in ruled_out:
                continue
            resolutions.append(
                ScreeningResolution(
                    protocol_id=protocol.protocol_id,
                    protocol_version=protocol.version,
                    question_id=protocol.question_id,
                    choice_id=choice_id,
                    status="ruled_out",
                    source="narrative_keyword",
                )
            )
    return resolutions


def validate_llm_resolutions(
    raw_resolutions: Any,
    problem_description: str,
    candidate_labels: Iterable[str],
) -> List[ScreeningResolution]:
    """Validate untrusted LLM output against eligible stable protocol IDs."""
    if not isinstance(raw_resolutions, list):
        return []
    protocols = {
        protocol.protocol_id: protocol
        for protocol in eligible_protocols(problem_description, candidate_labels)
    }
    validated: List[ScreeningResolution] = []
    seen = set()
    for item in raw_resolutions:
        if not isinstance(item, dict):
            continue
        protocol = protocols.get(str(item.get("protocol_id", "")))
        if protocol is None or item.get("question_id") != protocol.question_id:
            continue
        choice_id = str(item.get("choice_id", ""))
        choice = _choice_map(protocol).get(choice_id)
        status = item.get("status")
        if (
            choice is None
            or status != "disclosed"
            or choice_id in NON_SUBSTANTIVE_CHOICE_IDS
        ):
            continue
        key = (protocol.protocol_id, choice_id)
        if key in seen:
            continue
        seen.add(key)
        outcome = choice.outcome if status == "disclosed" else None
        validated.append(
            ScreeningResolution(
                protocol_id=protocol.protocol_id,
                protocol_version=protocol.version,
                question_id=protocol.question_id,
                choice_id=choice_id,
                status=status,
                source="narrative_llm",
                risk_level=outcome.risk_level if outcome else None,
                information_trigger=outcome.information_trigger if outcome else None,
                mandatory_category_ids=(
                    list(outcome.mandatory_category_ids) if outcome else []
                ),
            )
        )
    return validated


def merge_narrative_resolutions(
    keyword_resolutions: Sequence[ScreeningResolution],
    llm_resolutions: Sequence[ScreeningResolution],
) -> List[ScreeningResolution]:
    """Supplement keyword decisions while keeping them authoritative on conflict."""
    merged = list(keyword_resolutions)
    seen = {
        (resolution.protocol_id, resolution.choice_id)
        for resolution in keyword_resolutions
    }
    for resolution in llm_resolutions:
        key = (resolution.protocol_id, resolution.choice_id)
        if key not in seen:
            merged.append(resolution)
            seen.add(key)
    return merged


def narrative_results(
    resolutions: Sequence[ScreeningResolution],
    exclude_protocol_ids: Iterable[str] = (),
) -> List[ScreeningResult]:
    """Preserve every disclosed route, including equal-priority risks."""
    excluded = set(exclude_protocol_ids)
    disclosed = {
        (resolution.protocol_id, resolution.choice_id): resolution
        for resolution in resolutions
        if resolution.status == "disclosed" and resolution.protocol_id not in excluded
    }
    return [
        ScreeningResult(
            protocol_id=resolution.protocol_id,
            protocol_version=resolution.protocol_version,
            question_id=resolution.question_id,
            choice_id=resolution.choice_id,
            answer_source="narrative",
            risk_level=resolution.risk_level or "none",
            information_trigger=resolution.information_trigger,
            mandatory_category_ids=resolution.mandatory_category_ids,
        )
        for resolution in disclosed.values()
    ]


def select_questions(
    problem_description: str,
    candidate_labels: Iterable[str],
    completed_protocol_ids: Iterable[str],
    language: str = "en",
    limit: int = 3,
    resolutions: Sequence[ScreeningResolution] = (),
) -> List[ScreeningQuestion]:
    completed = set(completed_protocol_ids)
    eligible = [
        protocol
        for protocol in PROTOCOLS
        if protocol.enabled
        and protocol.protocol_id not in completed
        and _eligible(protocol, problem_description, candidate_labels)
    ]
    eligible.sort(key=lambda protocol: protocol.priority, reverse=True)
    questions: List[ScreeningQuestion] = []
    resolved_by_protocol: Dict[str, set[str]] = {}
    disclosed_by_protocol: Dict[str, set[str]] = {}
    for resolution in resolutions:
        resolved_by_protocol.setdefault(resolution.protocol_id, set()).add(
            resolution.choice_id
        )
        if resolution.status == "disclosed":
            disclosed_by_protocol.setdefault(resolution.protocol_id, set()).add(
                resolution.choice_id
            )
    for protocol in eligible:
        question_text = (
            protocol.question_es if language == "es" else protocol.question_en
        )
        disclosed_choice_ids = _disclosed_choice_ids(
            protocol, problem_description
        ) | disclosed_by_protocol.get(protocol.protocol_id, set())
        suppressed_choice_ids = (
            _suppressed_choice_ids(protocol, problem_description)
            | disclosed_choice_ids
            | resolved_by_protocol.get(protocol.protocol_id, set())
        )
        if disclosed_choice_ids:
            negative_choice_id = NEGATIVE_CHOICE_IDS_BY_PROTOCOL.get(
                protocol.protocol_id
            )
            if negative_choice_id:
                suppressed_choice_ids.add(negative_choice_id)
        choices = []
        for choice in protocol.choices:
            if choice.id in suppressed_choice_ids:
                continue
            choices.append(
                ScreeningChoice(
                    id=choice.id,
                    label=choice.es if language == "es" else choice.en,
                    translation_key=f"screening.{protocol.protocol_id}.choice.{choice.id}",
                    translations={"en": choice.en, "es": choice.es},
                )
            )
        if len(choices) < 2:
            continue
        if not any(choice.id not in NON_SUBSTANTIVE_CHOICE_IDS for choice in choices):
            continue
        questions.append(
            ScreeningQuestion(
                protocol_id=protocol.protocol_id,
                protocol_version=protocol.version,
                question_id=protocol.question_id,
                question=question_text,
                translation_key=f"screening.{protocol.protocol_id}.question",
                translations={"en": protocol.question_en, "es": protocol.question_es},
                choices=choices,
            )
        )
        if len(questions) >= limit:
            break
    return questions


def build_mandatory_categories(
    results: Sequence[ScreeningResult],
) -> List[MandatoryCategory]:
    categories: List[MandatoryCategory] = []
    seen = set()
    for result in results:
        for category_id in result.mandatory_category_ids:
            if category_id in seen:
                continue
            seen.add(category_id)
            categories.append(
                MandatoryCategory(
                    id=category_id,
                    label=CATEGORY_LABELS[category_id],
                    protocol_id=result.protocol_id,
                    rule_id=f"{result.protocol_id.removesuffix('.v1')}.{result.choice_id}",
                )
            )
    return categories


def build_effective_categories(
    labels: Sequence[Label], mandatory: Sequence[MandatoryCategory]
) -> List[EffectiveCategory]:
    """Union routes while preserving real confidence and unrelated model labels."""
    effective: List[EffectiveCategory] = []
    by_label: Dict[str, EffectiveCategory] = {}
    for label in labels:
        item = EffectiveCategory(
            id=label.id,
            label=label.label,
            confidence=label.confidence,
            mandatory=False,
            sources=["model"],
        )
        effective.append(item)
        by_label[label.label.casefold()] = item
    for category in mandatory:
        existing = by_label.get(category.label.casefold())
        if existing is not None:
            existing.id = existing.id or category.id
            existing.mandatory = True
            if "protocol" not in existing.sources:
                existing.sources.append("protocol")
            continue
        item = EffectiveCategory(
            id=category.id,
            label=category.label,
            confidence=None,
            mandatory=True,
            sources=["protocol"],
        )
        effective.append(item)
        by_label[category.label.casefold()] = item
    return effective


def candidate_label_strings(
    results_with_providers: Iterable[Tuple[str, object]],
) -> List[str]:
    """Retain all provider candidate labels, including those outside the final three."""
    labels: List[str] = []
    seen = set()
    for _provider, result in results_with_providers:
        if not isinstance(result, dict):
            continue
        for entry in result.get("labels", []):
            value = entry.label if isinstance(entry, Label) else entry.get("label")
            if value and value not in seen:
                seen.add(value)
                labels.append(value)
    return labels


def answers_for_model(answers: Sequence[ScreeningAnswer]) -> List[FollowUpAnswer]:
    """Use approved English text only to help LLM refinement; rules still use IDs."""
    validate_answers(answers)
    rendered = []
    for answer in answers:
        protocol = PROTOCOL_BY_ID[answer.protocol_id]
        choice_map = _choice_map(protocol)
        selected_labels = [
            choice_map[choice_id].en for choice_id in answer.selected_choice_ids()
        ]
        rendered.append(
            FollowUpAnswer(
                question=protocol.question_en,
                answer="; ".join(selected_labels),
            )
        )
    return rendered
