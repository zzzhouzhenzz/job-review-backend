import XCTest
@testable import ReviewCore

final class CompanyPresentationTests: XCTestCase {
    private func company(name: String = "Fictional Systems", data: [String: JSONValue] = [:]) -> Company {
        Company(id: "fictional", name: name, data: data, excluded: false,
                researchStatus: "none", note: "", revision: 1)
    }

    func testMissingAndMalformedDescriptionDoesNotUseRankingSummary() {
        XCTAssertNil(company().businessDescription)
        XCTAssertNil(company(data: ["description": .string(" \n\t ")]).businessDescription)
        XCTAssertNil(company(data: ["description": .number(3)]).businessDescription)
        XCTAssertNil(company(data: ["summary": .string("These jobs match the applicant.")]).businessDescription)
        XCTAssertEqual(company(data: ["description": .string("Builds tools for energy operators."),
                                      "summary": .string("These jobs match the applicant.")]).businessDescription,
                       "Builds tools for energy operators.")
    }

    func testDescriptionKeepsAtMostTwoSentencesAndNormalizesSpacing() {
        XCTAssertEqual(company(data: ["description": .string("  Builds data tools. \n\t Serves small teams. Third sentence. ")]).businessDescription,
                       "Builds data tools. Serves small teams.")
        XCTAssertEqual(company(data: ["description": .string("Builds data tools without a trailing stop")]).businessDescription,
                       "Builds data tools without a trailing stop")
    }

    func testDescriptionPreservesDecimalAndSentencePunctuation() {
        XCTAssertEqual(company(data: ["description": .string("Tools measure 3.5 volts. Teams use them daily! There is more.")]).businessDescription,
                       "Tools measure 3.5 volts. Teams use them daily!")
        XCTAssertEqual(company(data: ["description": .string("公司开发数据工具。它服务小型团队。更多信息。")]).businessDescription,
                       "公司开发数据工具。 它服务小型团队。")
    }

    func testLogoRequiresExplicitHTTPSURLWithHostAndNoCredentials() {
        XCTAssertEqual(company(data: ["logo_url": .string("https://assets.example/logo.png")]).logoURL?.absoluteString,
                       "https://assets.example/logo.png")
        for value in ["", " ", "logo.png", "//assets.example/logo.png", "https://", "https:///logo.png",
                      "https:// /logo.png", "https://%20/logo.png", "http://assets.example/logo.png",
                      "file:///logo.png", "data:image/png;base64,AA==", "javascript:alert(1)",
                      "https://user@assets.example/logo.png", "https://user:password@assets.example/logo.png",
                      "https://@assets.example/logo.png"] {
            XCTAssertNil(company(data: ["logo_url": .string(value)]).logoURL, value)
        }
        XCTAssertNil(company(data: ["logo_url": .bool(true)]).logoURL)
        XCTAssertNil(company().logoURL)
    }

    func testLogoIsNotDerivedFromJobBoardOrWebsite() {
        XCTAssertNil(company(data: ["board_url": .string("https://job-boards.example/fictional"),
                                    "website_url": .string("https://fictional.example")]).logoURL)
    }

    func testInitialsUseAtMostTwoGraphemes() {
        XCTAssertEqual(company().displayInitials, "FS")
        XCTAssertEqual(company(name: "  Fictional  Systems Studio  ").displayInitials, "FS")
        XCTAssertEqual(company(name: "Fictional").displayInitials, "F")
        XCTAssertEqual(company(name: " \n ").displayInitials, "?")
        XCTAssertEqual(company(name: "équipe 東京").displayInitials, "É東")
        let emoji = company(name: "👩🏽‍💻 Data Systems").displayInitials
        XCTAssertEqual(emoji, "👩🏽‍💻D")
        XCTAssertEqual(emoji.count, 2)
        XCTAssertLessThanOrEqual(company(name: "ß Data").displayInitials.count, 2)
    }
}
