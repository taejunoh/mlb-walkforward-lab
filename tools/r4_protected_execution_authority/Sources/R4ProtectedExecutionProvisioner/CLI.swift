import Foundation

enum CLIError: Error, Equatable { case invalidGrammar }

enum CLICommand: Equatable {
    case materialize(inputBundle: String)
    case verify
}

/// Deliberately closed command grammar.  It is parsed before any authority object is opened.
enum CLI {
    static func parse(_ argv: [String]) throws -> CLICommand {
        if argv == ["verify"] { return .verify }
        if argv.count == 3, argv[0] == "materialize", argv[1] == "--input-bundle", try absoluteNoAlias(argv[2]) {
            return .materialize(inputBundle: argv[2])
        }
        throw CLIError.invalidGrammar
    }

    private static func absoluteNoAlias(_ path: String) throws -> Bool {
        guard path != "/" else { return false }
        return (try? POSIX.absolutePathComponents(path)) != nil
    }
}
