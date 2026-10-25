import XCTest
@testable import ReviewCore

final class JobPresentationTests: XCTestCase {
    func testAssessmentParagraphBecomesCompleteSentenceBullets() {
        let text = "Backend ownership matches. Domain experience is unconfirmed."
        XCTAssertEqual(AssessmentText.bulletPoints(from: text),
                       ["Backend ownership matches.", "Domain experience is unconfirmed."])
    }

    func testAssessmentListsPreserveOrderDuplicatesAndQualifications() {
        let text = "  • Python or Go experience.\n\n- Production ownership is not confirmed.\r\n2. Python or Go experience."
        XCTAssertEqual(AssessmentText.bulletPoints(from: text),
                       ["Python or Go experience.", "Production ownership is not confirmed.", "Python or Go experience."])
    }

    func testAssessmentTechnicalTextAndFragmentsStayIntact() {
        for text in ["Python 3.11 and Node.js; hands-on scope is unknown", "C++ / C# experience", "U.S. work eligibility is unconfirmed."] {
            XCTAssertEqual(AssessmentText.bulletPoints(from: text), [text])
        }
        XCTAssertTrue(AssessmentText.bulletPoints(from: " \n\t ").isEmpty)
    }

    private func job(data: [String: JSONValue] = [:]) -> Job {
        Job(key: "fictional:sample", companyID: "fictional", jobID: "sample", data: data,
            revision: 1, decision: "pending", note: "", changed: false)
    }

    func testMissingMalformedAndEmptyRequirementsAreUnavailable() {
        XCTAssertTrue(job().requirements.isEmpty)
        let unavailable: [JSONValue] = [.null, .number(5), .bool(true), .object([:]),
                                        .string(""), .string(" \n\t "), .array([]),
                                        .array([.string(" "), .null, .number(2)])]
        for value in unavailable {
            XCTAssertTrue(job(data: ["requirements": value]).requirements.isEmpty, String(describing: value))
        }
    }

    func testStringRequirementsPreserveTheFullSourceText() {
        let text = "Experience operating distributed services.\nPython or Java; a degree or equivalent experience."
        XCTAssertEqual(job(data: ["requirements": .string(" \n" + text + "\n ")]).requirements, [text])
    }

    func testRequirementListPreservesOrderAndFiltersOnlyBlankOrNonTextEntries() {
        let values: [JSONValue] = [.string(" Python experience. "), .string(""), .bool(false),
                                  .string("Production service ownership.\nOn-call experience preferred."),
                                  .object(["requirement": .string("Do not infer this.")]),
                                  .string("Python experience.")]
        XCTAssertEqual(job(data: ["requirements": .array(values)]).requirements,
                       ["Python experience.", "Production service ownership.\nOn-call experience preferred.",
                        "Python experience."])
    }

    func testRequirementsNeverComeFromFitGapsOrRankerReasons() {
        let notes: [String: JSONValue] = [
            "strengths": .array([.string("Strong Python match.")]),
            "gaps": .array([.string("Kubernetes depth is unknown.")]),
            "fit": .string("Backend work matches."),
            "why_ranked": .string("Cloud experience is relevant."),
            "reason": .string("Selected by the ranker."),
            "description": .string("A full posting must not be relabeled as a requirements list."),
            "brief": .object(["fit": .string("A good match."), "gap": .string("Degree unknown.")])
        ]
        XCTAssertTrue(job(data: notes).requirements.isEmpty)
        var withRequirements = notes
        withRequirements["requirements"] = .array([.string("Experience designing APIs.")])
        XCTAssertEqual(job(data: withRequirements).requirements, ["Experience designing APIs."])
    }

    func testMissingBlankAndMalformedPayAndWorkSettingUseNotStated() {
        XCTAssertEqual(job().payDescription, "Not stated")
        XCTAssertEqual(job().workSettingDescription, "Not stated")
        let values: [JSONValue] = [.null, .number(200_000), .bool(false), .array([.string("Unknown")]),
                                   .object([:]), .string(""), .string(" \n\t ")]
        for value in values {
            let sample = job(data: ["salary": value, "arrangement": value])
            XCTAssertEqual(sample.payDescription, "Not stated")
            XCTAssertEqual(sample.workSettingDescription, "Not stated")
        }
    }

    func testPayAndWorkSettingKeepCompletePostingText() {
        let sample = job(data: ["salary": .string("  $150,000–$220,000 annual base; equity and bonus separate. \n"),
                                "arrangement": .string(" Hybrid; three office days per week. Remote eligibility is not stated. ")])
        XCTAssertEqual(sample.payDescription, "$150,000–$220,000 annual base; equity and bonus separate.")
        XCTAssertEqual(sample.workSettingDescription, "Hybrid; three office days per week. Remote eligibility is not stated.")
    }

    func testWorkSettingDoesNotInferTermsFromLocationOrShortBrief() {
        let sample = job(data: ["location": .string("Remote in the US"),
                                "brief": .object(["arrangement": .string("Hybrid"), "salary": .string("High pay")])])
        XCTAssertEqual(sample.payDescription, "Not stated")
        XCTAssertEqual(sample.workSettingDescription, "Not stated")
    }

    func testSourceGapsAcceptStringOrListWithoutShortening() {
        let text = "GPU operations experience is unconfirmed.\nDo not assume production ownership."
        XCTAssertEqual(job(data: ["gaps": .string(" \n" + text + " ")]).sourceGaps, text)
        XCTAssertEqual(job(data: ["gaps": .array([.string(" Cloud experience is unknown. "), .null,
                                                  .string(""), .number(3), .string("Go proficiency is unverified.")])]).sourceGaps,
                       "Cloud experience is unknown.\nGo proficiency is unverified.")
    }

    func testMissingEmptyAndMalformedSourceGapsAreUnavailable() {
        XCTAssertNil(job().sourceGaps)
        let values: [JSONValue] = [.null, .bool(false), .number(2), .object([:]),
                                   .string(" \n"), .array([]), .array([.string(" "), .null])]
        for value in values { XCTAssertNil(job(data: ["gaps": value]).sourceGaps) }
    }

    func testRoleDescriptionKeepsFullSourceTextAndRejectsMalformedValues() {
        let text = "Build reliable services.\nOwn design, testing, rollout and support."
        XCTAssertEqual(job(data: ["description": .string(" \n" + text + " ")]).roleDescription, text)
        XCTAssertNil(job().roleDescription)
        XCTAssertNil(job(data: ["description": .string(" \n")]).roleDescription)
        XCTAssertNil(job(data: ["description": .array([.string(text)])]).roleDescription)
    }

    func testSourceGapsAndRoleDescriptionDoNotUseOtherFields() {
        let sample = job(data: ["requirements": .array([.string("Requires Python.")]),
                                "fit": .string("Relevant backend work."),
                                "why_ranked": .string("Selected for infrastructure experience."),
                                "reason": .string("Selected by a ranker."),
                                "brief": .object(["work": .string("Build APIs."), "gap": .string("Go is unknown.")])])
        XCTAssertNil(sample.sourceGaps)
        XCTAssertNil(sample.roleDescription)
    }
}
